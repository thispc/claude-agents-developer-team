# The Studio — globally-persistent agents

*Blueprint and sprint plan. Working surface name: **The Studio** (the room your AI
teammates reside in between jobs). Replaces the round-table "Shape an idea" mode.*

The design rests on one fact already true of this codebase: **the parts that feel
like life are the parts that already cost nothing.** The scheduler orchestrates a
DAG on an 8-second loop with no model call; upkeep scans and ranks daily for free
because it only reads what was already recorded; `team.release` writes an agent's
memory note without an inference call. A Studio agent "living in the background"
is a free deterministic sweep over rows it already wrote, and it touches a model
only at discrete, size-triggered, capped, batched moments.

---

## 1. The token budget — the whole point

> **At rest, a Studio agent costs exactly zero tokens. Every model call is
> triggered by *accumulated work*, never by *elapsed time*, is batched to one
> call, runs on the cheap tier, and is bounded three ways: a per-agent cooldown,
> a per-tick ceiling, and a per-day hard budget.**

| Operation | Trigger | Free or bills | Cap |
|---|---|---|---|
| Working a task (existing) | scheduler dispatch | bills (unchanged) | existing `max_runs` |
| Inject memory into a worker | every dispatch | **free** — a string built into the prompt | char cap |
| Episodic write | task done / rework / escalation | **free** — reuses `team._gist`, no model | append-only row |
| Memory consolidation | episodes accumulated ≥ threshold | bills — **one** cheap call per due agent | `max_tokens`, per-tick cap, cooldown, daily budget |
| Evolution (model up/down) | free tick, enough recent runs | **free** — arithmetic over `runs` | one step, hysteresis |
| Agents talking (standup) | explicit, **off by default** | bills — **one** moderator call, never N² | rate-limited, daily budget |

**Nothing bills on a wall-clock timer.** Ten idle agents cost nothing; one busy
agent pays roughly one cheap call per dozen tasks. When the daily budget is spent,
consolidation and standup degrade to their deterministic fallback (truncation-based
memory, no standup) and the free parts keep running. A veteran carries what it
learned in project A into project B for zero extra tokens, because memory lives on
the global agent, not the per-project instance.

---

## 2. Data model

Global identity is a new `home_agents` table; the existing per-project `agents`
row becomes a *deployment* of one and gains a nullable `home_id`. The per-project
row stays load-bearing exactly because it is per-project — `team.assign/claim/
release`, the scheduler and `pick_model` all key on `(agent, project)` and one
`status`. One "Mike Ross" used in two projects at once needs two busy/idle states
and two run histories — that *is* two `agents` rows. So the home agent is the
durable **identity**; the project row is the **instance** a task is assigned to.
Memory flows up (instance episodes → home long-term memory); identity flows down
(home persona/model → instance at hire). Old rows get `home_id = NULL` and behave
exactly as today.

Four new tables: `home_agents` (identity + one current model + lifetime counters),
`home_episodes` (append-only raw events, written free), `home_memory` (the
compressed, bounded blob a worker sees), `home_evolution` (the model-change audit
log). One migration: `ALTER TABLE agents ADD COLUMN home_id INTEGER`.

---

## 3. Memory — three tiers, model touches only the middle

```
  episodic (home_episodes)     →   long-term (home_memory)      →   worker context
  append-only, FREE                compressed blob, BOUNDED           inlined, FREE
  (deterministic team._gist)       (1 cheap call when N pile up,       (it is just a string)
                                     truncation fallback)
```

Compression is **additive-then-swap**: the summary is written and only then are the
raw episodes marked consolidated, so a failed compression never loses anything. The
trigger is **size, never time** — an idle agent accumulates nothing and is never
due. Retrieval is O(1): the small blob is inlined through the existing
`team.system_addendum` seam; no embeddings, no vector store.

---

## 4. Evolution — deterministic, zero tokens

Arithmetic over the `runs` rows the platform already writes, walking the real
ladder `FALLBACK_ORDER = [haiku, sonnet, opus]`. **Upgrade** when an agent keeps
hitting work its model can't clear (high escalation + rework, enough volume);
**downgrade** when a cheaper model would land the same work first-pass. One step
per change, hysteresis so it can't flap, never past a locked model, never straight
to Opus. It rewrites only `home_agents.model`, which reaches the instance at next
hire — and `pick_model` already ranks the teammate's own model *below* escalation
and manager override, so evolution sets a better starting point without ever
overriding a live correction.

---

## 5. The Studio UI (Phase 2)

A warm room seen in elevation, built from the retiring round-table machinery
(positioned DOM nodes, `speechQueue`, `bubble-in`/`talk`/`glow`, `seatAngles`).
Agents are characters standing on a floor: drag them with Pointer Events (bench ↔
floor = hire/bench), name them by typing on them, give traits through a radial
bloom of petals rather than a form. Mood is derived from **real signals only** —
pine pool = busy, amber = throttled, **brick wash = needs you** (the one place
brick correctly lands on an agent, preserving the meaning rule). Memory motes rise
onto a shelf and visibly compress into denser rings. A new `.studio`-scoped dark
token layer, additive so the rest of the app is untouched. Every ambient motion
gated by `prefers-reduced-motion`; drag and state changes work without it.

---

## 6. The sprint — ordered

**Phase 1 — the token-bounded foundation (this is the priority):**
1. `db`: four `home_*` tables + the `agents.home_id` migration + accessors.
2. `home.py`: CRUD, name assignment, `describe()`, and the **free background tick**.
3. `tuning`: the Studio knobs (thresholds, caps, the daily budget) — each a knob, not a literal.
4. Episodic write (free): extend `team.release` and the manager judge path.
5. `memory.py`: size-based consolidation + one cheap batched call + truncation fallback; inject via `system_addendum`.
6. `evolution.py`: deterministic up/down from `runs` metrics + hysteresis + audit log.
7. Wire `home.loop()` into `main.lifespan`.
8. API: `/api/home` CRUD, `/use`, `/memory`, `/evolution`, `/consolidate`, `/budget`.
9. **Ironclad tests** — led by the token-cost tests (§7).

**Phase 2 — the Studio, and retire the round table:**
10. `standup.py` (off by default): one moderator digest, never N².
11. The Studio surface; retire `#modeShape` and the `#plan` circle, keeping their engine for standup and planning.

---

## 7. The test discipline — how "ironclad" is proven offline

The suite has no live API (every credential is blanked), so cost is proven by
**counting would-be model calls against a fake provider, then reading the source to
prove no other door to a model exists.** A `provider_spy` monkeypatches the two
choke points (`providers.complete`, `launcher.dispatch_task`) and a hard-fail fake
raises if real network is ever touched. Then, for every cost invariant, a counting
test *and* a structural test — because a counter only proves the paths you
exercised, and a second unmetered door passing a counting test is the exact
false-green this is paying to avoid.

The invariants pinned up front:
1. Studio spends only via the two choke points (structural).
2. Every completion has an explicit `max_tokens`; every session an explicit `max_turns`.
3. A `max_talk_hops` cap and an addressed-only wake rule bound conversation to O(K).
4. A compression trigger + persisted high-water mark; compression is additive-then-swap.
5. A deterministic evolution rule with hysteresis, moving only along the resolved ladder.
6. Owner scoping on the agent row and on memory retrieval.
7. All thresholds are tuning knobs with a rationale.

Live-only, honestly flagged and **not** part of "ironclad": that a *real* model's
compression is genuinely good, that a real cheaper model underperforms enough to
justify an upgrade, actual token accounting. The cost *guarantees* are entirely
provable offline — which is the point, because the owner's first constraint must be
enforced on every commit, not in a live run nobody dares repeat.
