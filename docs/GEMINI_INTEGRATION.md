# Running on Gemini: what it takes

Scoped from a full read of the code. The short version: **planning on Gemini is
essentially free; building on Gemini is a rewrite of the worker.**

---

## What you have to supply

One thing: a **Google AI Studio API key** (`AIza…`), either pasted into Settings →
Other providers → Google Gemini, or set in `.env` as `GEMINI_API_KEY` — or
`GEMINI_KEY`, which is accepted as an alias because that is what people actually
write. Operator `.env` keys reach the **root account only**; every other user
supplies their own.

There is no OAuth or subscription equivalent — Gemini is API-key-only, which has
a consequence worth naming up front: **every Gemini turn bills real money.** The
whole "runs on your subscription" economics of this platform is Anthropic-specific.

Stored per user in `users.settings` as `gemini_api_key` ([auth.py:134](../conductor/app/auth.py#L134)),
sent as an `x-goog-api-key` header ([providers.py:184](../conductor/app/providers.py#L184)).
Never an env var today.

> **Note:** until just now the Settings dialog *collected* this key and then threw
> it away — the Save handler enumerated three field names by hand and dropped the
> OpenAI and Gemini ones. Fixed, with a contract test that fails if any input in
> the form isn't submitted.

---

## What works today

**Plan mode's round table**, and **recruiting** (stage 1, done — the planner now
goes through `providers.py`, so a Gemini-only user gets a real domain-appropriate
team instead of silently falling through to the keyword heuristic).

**Nothing else.** Workers are Claude-only: `worker/worker.py` is built on the
Claude Agent SDK and `FALLBACK_ORDER` is three Claude models. A user holding
*only* a Gemini key can plan a project and then watch every task fail at dispatch,
because the credential gate checks for Anthropic keys specifically.

### Model ids rot, so don't pin them

`gemini-2.5-flash` was hard-coded in `providers.py` and had **already been
retired**: `ListModels` still advertises it, but `generateContent` answers *"no
longer available to new users"*. Prefer the `-latest` aliases.

Verified against a real key: `gemini-flash-latest`, `gemini-3.5-flash`,
`gemini-flash-lite-latest` and `gemini-3.1-flash-lite` all answer.
`gemini-pro-latest`, `gemini-3-pro-preview` and `gemini-2.0-flash` return **429**.

### A 429 saying `limit: 0` is not a rate limit

Google returns the same `RESOURCE_EXHAUSTED` for "you are going too fast" and for
"your plan includes zero of this model". The second says **`limit: 0`** with quota
ids ending `-FreeTier`, and no amount of waiting clears it — it means billing is
not enabled on the key's Google Cloud project. Generating a new key does nothing.

`providers.not_entitled()` detects this and **fails fast instead of retrying**,
because retrying an entitlement wall wastes the caller's time and teaches the
platform to treat a billing problem as a blip.

### Google reports an invalid key as 400, not 401

`400 INVALID_ARGUMENT / "API key not valid"`. Checking only for 401/403 made a
typo'd key report as "authenticated, but no model would answer".

### Free tier throws transient errors constantly

`providers.complete()` now retries 408/429/500/502/503/529 three times with
jittered backoff, honouring `Retry-After` *and* Google's `error.details[].retryDelay`
(which is where Google actually puts it — it never sends the header). Before this,
a single 503 permanently silenced a round-table seat. Expect to need it: a live
recruiting call hit 503 "high demand" on the first two attempts and succeeded on
the third.

---

## Why the worker is hard

The worker's entire agent harness is nine lines of `ClaudeAgentOptions`. That
compactness hides what the SDK is doing:

| What the SDK provides | Gemini equivalent |
|---|---|
| The think → call tool → observe loop | Function calling exists, but it's a *chat* loop. Turn counting, history, and **context compaction** are yours. A 120-turn session with full `Bash` output overruns even a 1M window |
| `Read` `Write` `Edit` `Glob` `Grep` `Bash` | **None exist.** All six must be written with JSON schemas. `Edit` needs exact-match + uniqueness or the model silently corrupts files; `Bash` needs timeouts, output truncation and cwd pinning |
| Cost in USD | Gemini returns *tokens*. Needs a hand-maintained price table that will drift |
| `ask_teammate` (nested MCP call) | Straightforward — it's just "call a model once", which `providers.complete()` already does |
| Text vs tool-use block stream | Maps cleanly onto Gemini's `text` / `functionCall` parts |
| Error text | Yours to format so the existing rate-limit matchers still fire |

Roughly **450–600 new lines**, of which the six tools are the bulk.

**Check this first:** Google's **Gemini CLI in headless mode** is the structural
analogue of the Claude Code CLI that the Agent SDK wraps, and already ships file
and shell tools. If its non-interactive output is machine-readable, this whole
section collapses to an adapter — days instead of weeks. Verify that before
writing a line of the loop.

---

## Sharp edges

1. **Credential isolation.** `owner_credentials()` blanks Anthropic variables
   explicitly because the worker env is `{**os.environ, **env}` — anything not
   blanked is inherited from the operator's shell. Adding Gemini without adding
   `"GEMINI_API_KEY": ""` to that blank dict reproduces a leak this codebase has
   already had once.

2. **No dollar cap.** The budget check is gated on `config.ANTHROPIC_API_KEY`
   being set, on the reasoning that subscription costs are fake. A subscription
   user who adds a Gemini key therefore gets **no spend limit at all**. This must
   be fixed *before* any Gemini worker runs, not after.

3. **Throttles will look handled and won't be.** `note_rate_limit()` regexes for
   `retry-after` and `try again in N minutes`. Google returns
   `RetryInfo.retryDelay: "38s"`, which matches neither — so every Gemini rate
   limit silently takes the 300-second default.

4. **Safety filters mid-session.** A `finishReason: SAFETY` can land on ordinary
   security code — auth flows, crypto, anything a test names "attack" — and
   destroy an hour of work at turn 80. Planning already handles the prompt-level
   case; the worker needs `finishReason` handling for `SAFETY`, `RECITATION`,
   `MAX_TOKENS` and `MALFORMED_FUNCTION_CALL`.

5. **`FALLBACK_ORDER` is Claude-only.** Falling back to Claude for a Gemini-only
   user is a guaranteed failure, and the all-models-cooling check would wrongly
   park a Gemini task because three Claude models are throttled.

---

## The staged plan

### Stage 1 — planner on `providers.py` ✅ done

`planner.py` is one prompt in, one JSON array out, `max_turns=1`, every built-in
tool disallowed, with a keyword fallback already in place. Swapping its `query()`
for `providers.complete()` is near-mechanical.

**Buys:** a Gemini-only user gets real team recruitment instead of silently
falling through to the keyword heuristic (the current failure is invisible — the
exception is swallowed). Proves the settings → provider plumbing on a low-stakes
path.

**Doesn't buy:** any Gemini-written code. That user still can't run a project.

### Stage 2 — manager on `providers.py` (2–3 days)

The manager is already tool-restricted to ~20 closed MCP tools with no file or
shell access, each a Python function taking a dict and returning text. They map
onto Gemini function declarations with schema translation, no reimplementation.
You still hand-write the loop.

**Buys:** the real prize — a Claude outage degrades the platform to Gemini
management instead of stopping it. This is the failover item in
[IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md).

### Stage 3 — Gemini workers (1.5–3 weeks)

Only after checking Gemini CLI headless. The 500 lines aren't the hard part; the
hard part is that the Claude SDK encodes accumulated behaviour — `Edit` semantics,
output truncation, compaction, tool-error recovery — that you only discover you
needed by watching agents fail on real repos. Budget most of the time for
iteration, not typing.

Per-role provider choice ("backend on Gemini, tester on Claude") lands here: the
roster select currently offers exactly two options, hard-coded to Haiku and Sonnet.

---

## Honest summary

"Any role on any provider" is **true for planning, false for building**, and the
gap is a genuine multi-week rewrite rather than a config change. Stage 1 and 2
are worth doing on their own merits for resilience. Stage 3 is worth doing only
if provider independence for code-writing is a product requirement — the cost is
real, and Gemini gives up the subscription economics that are this platform's
sharpest advantage.
