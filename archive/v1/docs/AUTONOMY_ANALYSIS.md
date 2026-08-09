# Can this run unattended for a day? An honest assessment

The goal, stated plainly: **human-grade efficiency and reliability from agents,
running all day in parallel without asking you anything, riding out rate limits,
with any role pluggable to any provider you hold a key for.**

This is where that stands, what the research says to spend compute on, and what
is still missing. Written after reading the weather-app run end to end.

---

## What the weather run actually showed

| | |
|---|---|
| Runs consumed | 12 of 40 |
| Wall clock | ~2.5 h across several sittings |
| Delivered | Working app, 2 PRs merged, 7/7 smoke checks |
| Wasted | ~6 attempts on one task |

Every unit of that waste came from **infrastructure, not model quality**:

1. A contest's output was invisible to the manager (fixed) — cost ~4 attempts.
2. Concurrent rivals raced on `git push` (**still open**) — 3 retries observed.
3. Conductor restarts orphaned in-flight work (fixed) — cost ~2 attempts.
4. The manager could not answer you, so you could not tell any of this was
   happening (fixed).

**The models were not the bottleneck. The harness was.** That matters for how you
spend the next ten days: more compute would not have helped this run at all.

---

## What the research says to spend compute on

From the papers gathered in [ROUNDTABLE_DESIGN.md](ROUNDTABLE_DESIGN.md):

**Do not buy more debate.** Multi-agent debate does not reliably beat one good
model, and at matched compute plain resampling scales better per token. Adding
rounds is the worst available use of your credit.

**Do buy more candidates — but only with a real verifier.** Best-of-N with
selection is the pattern that pays: CodeMonkeys got **66.2% vs 62.8%** for the
best single contributor purely from selecting over a pooled candidate set. That
is your contest feature, and it is the right place to spend.

**The verifier is the ceiling, and it is a hard one.** *Inference Scaling fLaws*
shows an imperfect verifier caps resampling accuracy **regardless of budget** —
false-positive rate does not shrink with N. And JudgeBench found strong judges
perform "only slightly better than random" on hard pairs.

> **The single highest-value change for reliability is giving the manager
> executable evidence — tests, builds, type-checks — instead of asking it to
> judge prose.** Everything else is bounded by that.

**More calls can make things worse.** Voting systems are non-monotonic in the
number of calls: accuracy rises then falls, because extra calls help easy
queries and hurt hard ones. "Burn all the compute" is not a strategy.

---

## Gap analysis against all-day unattended running

### Fixed in this pass

| Gap | Was | Now |
|---|---|---|
| Cooldowns forgotten on restart | in-memory only; every restart hammered a throttled model | persisted in `model_cooldown`, restored at startup |
| All models throttled | dispatched anyway, burning an attempt | task stays `planned`, waits for the soonest window, says how long |
| Manager stalls on a question | blocked **60 min** even with full autonomy | 5 min grace (`AUTONOMOUS_QUESTION_GRACE`), then decides and records the assumption |
| Manager could not answer you | no mechanism | `reply_to_boss` with live facts |
| Feed froze after 500 events | showed the *oldest* 500 | shows the newest |

### Still open, in priority order

**1. ~~The manager judges prose, not evidence.~~ DONE.**
The platform now runs the project's own test/build command itself after each
worker finishes — in the worker *process*, not the agent session, so the model
cannot summarise or invent the result. The raw exit code and output lead the
report, and `merge_pr` refuses a branch whose checks failed (override needs the
boss). Detection is conservative: npm test/build/lint, pytest, go test, cargo
test, make test. "No command declared" is reported as *unverified*, never as
passing.

**2. Workers are Claude-only.** `providers.py` gives plan mode Anthropic + OpenAI
+ Gemini, but `FALLBACK_ORDER` is three Claude models and the worker is built on
the Claude Agent SDK. So "any role on any provider" is **true for planning, false
for building**.

Be clear about the cost: a worker needs an agentic loop with file and shell
tools. The Claude SDK supplies that; OpenAI and Gemini would each need their own
harness. This is days of work, not hours. **The pragmatic middle:** route the
*manager* and *planner* through `providers.py` (they only need text in, text
out), so a Claude outage degrades you to Gemini planning rather than a dead
platform. Workers stay Claude until the harness exists.

**3. Concurrent rivals race on `git push`.** Observed 3 times. Two rivals pushing
to the same remote contend even on different branches. This corrupts exactly the
feature (contests) the research says is worth spending on. Fix: serialise the
push, or give each rival its own clone with a retry-with-backoff on ref lock.

**4. Run cap of 40 is a day-long-run cap of about three hours.** For an overnight
run set `max_runs` to 200+ in Advanced limits. Note the non-monotonicity warning:
raise the cap to let work *finish*, not to let it retry indefinitely.

**5. No cross-provider failover for the manager.** If Claude is throttled, the
manager stops even when you hold a Gemini key. Follows from gap 2.

---

## What to do with ten days of compute

Ranked by expected return, given the evidence:

1. **Fix the push race** (hours). It corrupts contests, which is the one
   compute-spending pattern with real support behind it.
2. **Give the manager executable evidence** (a day). The verifier bounds
   everything; this is the difference between "reliable like a human" and
   "confident like a model".
3. **Run contests on the genuinely ambiguous tasks only** — research, design,
   architecture. Not on scaffolding or CRUD, where one competent attempt is
   enough and a contest just burns runs.
4. **Route the manager through `providers.py`** (a day) so a Claude limit
   degrades the platform instead of stopping it.
5. **Then** spend the rest on long unattended runs with `max_runs` raised — but
   measure. A run that consumes 200 units and ships nothing is a result too, and
   the logs now tell you which of the four failure modes above caused it.

**What not to do:** add debate rounds, add seats to the round table, or raise
contest rivals above 3. All three are refuted or unsupported at matched compute.

---

## The honest summary

The platform is **closer to unattended than it looks, and further from
"any AI in any role" than the plan-mode UI implies.** Overnight running is now
plausible: cooldowns persist, throttles are ridden out rather than fought,
questions no longer stall the night, and you can see what happened. What is not
yet true is provider independence for the *building* half, and evidence-based
verification — and the second of those is what actually separates agent
reliability from human reliability.
