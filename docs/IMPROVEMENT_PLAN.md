# What we decided from the research, and what we're building

Every multi-agent idea we considered, the evidence for or against it, and the
verdict. Written so we stop re-litigating settled questions.

Sources are in [ROUNDTABLE_DESIGN.md](ROUNDTABLE_DESIGN.md) and
[AUTONOMY_ANALYSIS.md](AUTONOMY_ANALYSIS.md).

---

## The one finding everything else hangs off

> **The verifier is the ceiling, and the search around it barely matters.**

Three independent results say the same thing:

- *Inference Scaling fLaws*: an imperfect verifier caps resampling accuracy
  **regardless of budget**. The false-positive rate does not shrink with N.
- Changing only the value function — same search, same budget — moved results
  **7 points**. Changing the search algorithm moved them **<1 point**.
- DeepSeek-R1 abandoned MCTS entirely because the value model was the hard part.

So the ranking rule for this project is: **anything that makes the judgement more
grounded beats anything that makes the search wider or deeper.** Compute spent on
more opinions is compute not spent on better evidence.

---

## The decisions

| Idea | Verdict | Why |
|---|---|---|
| Run the project's real tests and judge on the exit code | **Build** ✅ done | The verifier is the ceiling. Run by the worker *process*, so the model can't summarise or invent it |
| Best-of-N rivals + selection (contests) | **Keep** ✅ done | CodeMonkeys: **66.2% vs 62.8%** for the best single contributor, purely from selecting over a pooled set |
| Blind, shuffled, prefiltered judging | **Keep** ✅ done | Judges are measurably sensitive to order, favour their own model family, and selection over a pool containing failures underperforms plain majority voting |
| Contests only on ambiguous work | **Build** ⬜ | On scaffolding and CRUD one competent attempt is enough; a contest just burns runs |
| BFS — finish all rivals, then judge | **Keep** ✅ done | CodeTree, the one direct ablation *on code*: BFS > DFS. "Exploring diverse strategies beats iteratively refining one." Optimal width 4–5, depth 2 |
| Whole team votes on contests instead of the manager | **Reject** ❌ | 162 roles × 2,410 questions: domain-matched personas **+0.4pp**. Expert personas on GPQA: **no significant improvement**. Out-of-domain personas made one model **refuse 10.56/25 trials**. Zero studies ablate judge persona on code |
| DFS / speculative downstream work | **Reject** ❌ | Essentially unstudied. Agent-step speculation accuracy **0.46–0.76**, buys **≤20–37%** latency, and only under an "idempotent/reversible operations only" envelope that excludes coding and deploys |
| More debate rounds | **Reject** ❌ | MAD does not reliably beat one good model; its gains are reproducible by tuning a single unrelated hyperparameter (~15%). At matched compute plain resampling scales better per token |
| More seats at the round table | **Reject** ❌ | Nine judges across seven model families = **~2.18 effective independent votes**. You pay 9×, you get 2× |
| Rivals above 3 | **Reject** ❌ | Voting systems are non-monotonic in call count: accuracy rises then falls. Extra calls help easy tasks and hurt hard ones |
| Reuse a math step-verifier (PRM) | **Reject** ❌ | Math PRMs score **50.8%** on tool-use step discrimination — a coin flip. Step verifiers do not transfer across domains |
| A dedicated step-level verifier | **Defer** ⏸ | SWE-PRM works (40 → 50.6% on SWE-bench Verified) but at **9× cost** — ~$23.20 per additional solved instance. And weak models used as verifiers made results **worse**. Revisit only if runs are cheap |
| Self-MoA in diverge mode (one strong model sampled N times) | **Keep** ✅ done | Self-MoA beat mixed-model MoA. This is why the homogeneity warning **inverts** between debate and diverge modes |
| Better aggregation over more diversity | **Build** ⬜ | Dipper: aggregation method **+8.24pp**, prompt diversity **+1.55pp**. The aggregation half is worth ~5× the diversity half |

---

## Why one sprint cost so much — the mars-rover numbers

Measured from the real run, not estimated.

| | |
|---|---|
| Agent runs | **8** for ~1.5 tasks of net progress |
| Wall clock | 4 hours |
| Runs on the cheap model | **0** |
| Runs lost to non-quality causes | **6 of 8** |

The answer to *"faster iterations or quality iterations?"* is **neither — you were
not paying for iteration at all.** Every expensive thing here was waste:

**1. Every single run was sonnet-5.** Not one Haiku run happened. The recruiter
assigned `lead` to 4 of 6 roles, and the two `worker`-tier roles hadn't been
reached yet. The platform's whole thesis — cheap iterations, orchestrated well —
was never actually exercised.

**2. Four retries were capacity, not quality.** Anthropic's subscription message
is `You've hit your session limit · resets 3pm`, which matched **none** of the
rate-limit markers. So a capacity death was classified as a *quality* failure and
pushed the task up the escalation ladder. The manager noticed and reassigned by
hand, writing *"both prior attempts died on a session/rate limit (not a quality
failure)"* — it was working around the harness. ✅ fixed

**3. Escalation was a no-op.** It returned a fixed `ESCALATION_MODEL`, so a task
the roster had already put on sonnet-5 "escalated" to sonnet-5. The retry was
spent changing nothing. ✅ fixed — escalation now steps up from whatever failed.

**4. Every retry started cold.** A retry only received manager *feedback*, which a
session-limit death never has. So the agent re-derived everything it had already
worked out; the branch kept the files but not the reasoning. With 6 of 8 runs
being retries, this was the single largest line item. ✅ fixed — a retry now
carries the previous attempt's own report and is told to continue, not restart.

**5. There was no verifier.** The manager merged both foundational PRs reasoning
*"No test command exists, so there are no failing checks to override."* On a project
with no test command, every quality claim in this document stops applying.

**So: quality iterations — but the cheap tier should be doing most of them, and
none of the budget should go to redoing work that was already done.** The four
fixes above address the waste. The recruiter's `lead` bias and the missing
verifier are what remain.

---

## The ranked plan

Ordered by expected return per unit of work, not by appeal.

### 1. Stop losing runs to the harness — *highest return, least glamorous*

The weather-app run consumed 12 runs and wasted ~6. **Every unit of waste was
infrastructure, not model quality.** More compute would not have helped it at all.

- ✅ Contest output invisible to the manager — fixed
- ✅ Restart orphaned in-flight work — fixed
- ✅ Manager couldn't answer the boss — fixed
- ✅ Feed froze after 500 events — fixed
- ✅ A blocked plan still read as "running" — fixed
- ✅ **Concurrent rivals race on `git push`** — rebase-on-non-fast-forward, plus
  jittered backoff. The fixed 3/6/9s schedule was marching rivals straight back
  into the same collision on every retry
- ✅ **A transient provider error killed a seat outright.** `providers.complete()`
  retries throttles and capacity blips, honouring the provider's own delay hint

### 2. Make the verifier better, since it is the ceiling

- ✅ Run tests/build/lint in the worker process; `merge_pr` refuses a failed branch
- ✅ **Report *what* failed, not just that it failed.** The judge saw a pass/fail
  bit; it now gets the failing test names, the assertion text and the tool's own
  count line, extracted from the whole output rather than the tail we keep — and
  is told to quote them when sending work back
- ⬜ **Treat "no test command declared" as a first-class blocker**, not as silent
  `unverified`. An unverifiable project is one where every downstream number in
  this document stops applying

### 3. Spend contests where ambiguity actually lives

- ⬜ Default `compete` off for scaffolding/CRUD roles, on for research,
  architecture and design. Same run budget, concentrated where selection pays

### 4. Improve aggregation, not diversity

- ⬜ When rivals disagree, the manager currently picks one whole branch. Worth
  more (+8.24pp vs +1.55pp) is **synthesising** — take the winner and graft the
  specific things the runners-up did better. Cheap: one extra call, no extra rivals

### 5. Provider failover for the manager

- ✅ **Planner** on `providers.py` — recruiting now runs on whichever provider the
  user holds a key for, and logs when it falls back instead of failing silently
- ⬜ **Manager** on `providers.py` — the real prize: a Claude limit then degrades
  the platform to Gemini management instead of stopping it. See
  [GEMINI_INTEGRATION.md](GEMINI_INTEGRATION.md)

---

## What we are deliberately not building

Kept explicit so it doesn't get proposed again:

- **Team-wide voting on contests.** N× cost, ~0 gain, and out-of-domain personas
  actively degrade to refusals.
- **Deeper or speculative search.** Unstudied, and outside the safety envelope
  that makes speculation defensible anywhere else.
- **More rounds, more seats, more rivals.** All three are refuted or unsupported
  at matched compute, and the non-monotonicity result means they can make things
  worse, not merely cost more.

If a future idea is "add more model calls arranged differently", the prior is that
it does nothing. It needs its own evidence before it gets built.

---

## How we'll know any of this worked

The failure modes are now distinguishable in the logs, so measure per run:

| Metric | Why |
|---|---|
| Runs consumed ÷ tasks merged | The efficiency number. The weather run was ~2.0; human-grade is closer to 1.2 |
| Runs lost to harness faults | Should trend to zero. Anything else is a bug, not a model limit |
| Merges where verification actually ran | If this isn't near 100%, the ceiling result means nothing else here applies |
| Contest wins that later needed rework | Measures whether selection is real or noise |

A run that consumes 200 units and ships nothing is a result too — as long as the
logs say which of those rows caused it.
