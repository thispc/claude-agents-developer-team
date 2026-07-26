# The Human Model — a blueprint for living agents (v2)

*A brainstorm-grade design for modelling a human as code: numbers, thresholds, and
learned rules inside a Python class that grows through experience. This is context,
not implementation — the build order at the end sequences it into later waves. Every
section names the module it extends and states its cost, so nothing here is an orphan
idea or a hidden invoice. It is the next layer on the pods, scenes, sealed artifacts,
and budgeted memory that already run and pass tests — a maturation, not a rewrite.*

The one law that governs all of it:

> **The model proposes; the code disposes.** An LLM only ever *proposes* a change, as
> a typed packet; deterministic code applies it, clamped. Break this and you get
> either a diverging lunatic or a runaway invoice.

**What changed in v2** (owner review): a real feature-flag system (`switch_drama_off`
and friends), a **competency graph** replacing flat per-domain counters, per-component
refinements for design/efficiency/security/redundancy, an honest security threat
model, an explicit map onto the tables we already ship (so we don't build a second of
anything), and a worked end-to-end trace of a single scan.

---

## −1 · World Config — the master switches

Not every world wants drama. A casino wants betrayal and bluffing; a **dev team wants
competent professionals who don't have feelings about a code review.** So behaviour is
governed by flags, resolved in layers exactly like `tuning.py` already resolves knobs:

```
WORLD default  <  SCENE override  <  AGENT override
```

A scene can force seriousness on everyone in it; a single agent can be tuned an
eccentric inside a serious world. Flags are grouped, and ship with **presets** that
flip a whole bundle:

| Group | Flags | `serious` | `sandbox` | `theatre` |
|---|---|---|---|---|
| **Learning** | `rule_compiler`, `skill_growth` | on | on | on |
| **Affect** | `emotions`, `drives` | dampened | on | amplified |
| **Drama** | `theory_of_mind`, `gossip`, `deception`, `mood_volatility`, `self_deception` | **off** | on | on+ |
| **Security** | `secrets_circles`, `ledger` | on | on | on |
| **Life** | `mortality` (decline), `inheritance` | off | on | on |
| **Memory** | `memory_level` (off · lite · full) | full | full | full |

- **`switch_drama_off`** is the one-liner the owner asked for: it selects the
  `serious` preset — Drama group **off**, `mortality` off, `mood_volatility` capped —
  while keeping learning, memory, skills, and secrets fully on. Result: agents that
  remember, improve, and keep confidences, but never sulk, scheme, or age out. The
  dev team, minus the soap opera.
- Flags are **read at the one place each behaviour lives** (a disabled group is a
  cheap early-return), so turning drama off doesn't just hide it — it stops computing
  it. Off is free.

> Extends: `tuning.py` (the exact layered-resolution pattern) + a per-scene column and
> a per-agent override in the psyche. New: a `WorldConfig` facade and the presets.

---

## 0 · Time is a scan (and not every scan is equal)

Time is measured in **LLM scans**. One scan — one `τ` (tau) — is a single
deliberation. Age = lifetime scans; a pin (§4) is the state snapshot each scan leaves.

**Refinement over v1:** a scan is not always worth `+1`. A cheap reflex teaches almost
nothing; a hard, high-stakes deliberation teaches a lot. So experience accrues by
**engagement weight**, derived from which tier fired and the stakes:

```
e = tier_weight × stakes          # reflex 0.2 · habit 0.5 · deliberation 1.0, × salience
```

This is the fix for "domain +1 was weird": you don't get smarter by *showing up*, you
get smarter by *engaging hard*. Free — it's a number you already have from the tier
and the salience gate.

---

## 0.25 · The Competency Graph — subdomains, done right

Flat `experience[domain]` was too blunt. Competencies form a **graph**: a
materialised-path tree as the spine (`eng.backend.auth`, `law.contract.ma`) with
optional **cross-edges** where real skill transfers between branches.

```
eng ── backend ── auth ── oauth
   └── frontend        │
law ── contract ── m&a │
                       ▼
                 negotiation   ← a cross-edge: shared by law.contract AND sales
```

- A scan credits its **leaf** competency `xp[leaf] += e`, then **propagates** the
  gain to neighbours along edges, decayed and capped at 1–2 hops (working M&A makes
  you a bit better at contract law and a little at negotiation — cheap, bounded):
  `xp[n] += e · transfer[leaf→n]`.
- **Proficiency at a node** = the saturating curve over *effective* xp (its own plus
  propagated): `skill[n] = 1 − e^(−k · xp_eff[n])`.
- **Skills fade.** Unused competencies decay very slowly (`skill_forget`), so mastery
  you never practise erodes — realistic, and it keeps the graph honest rather than
  ever-accumulating.
- **Recruiting reads this graph**: "who is strongest at `eng.backend.auth`" is a node
  lookup with ancestor fallback, which is exactly the "cast by fit" the manager
  already wants.

Start as the tree (a `path` string — no join needed); add cross-edges only where a
transfer genuinely exists. That is the "more complicated data structure, if needed,"
kept as simple as the need allows.

> Extends: `psyche.py` (`maturation` curves). New: a `skills` store keyed by
> competency path, plus a small static `competency_edges` table for cross-transfer.

---

## 0.5 · The Rule Compiler — the centrepiece

A human deliberates over how to hold a fork exactly once; then it's a reflex. **Our
agents compile experience into reflexes the same way,** and that is the moat: an agent
gets *cheaper* as it gets *better*.

| Tier | What it is | Cost |
|---|---|---|
| **0 · Reflex** | shipped deterministic rules (seed = today's `_rule_appraise`) | free |
| **1 · Habit** | rules the agent *compiled* from its own past deliberations | free |
| **2 · Deliberation** | one bounded LLM scan — the only thing that spends | 1 call |

**How a habit forms.** When Tier-2 keeps returning the same shape of consequence for
the same shape of signal, the `(pattern → X-template)` pair is written as a **typed
rule row** — data interpreted by a fixed evaluator, never a string that gets `exec`'d
(the RCE door stays shut).

**Refinements over v1 — a rule engine that doesn't rot:**
- **Probation & shadow-mode.** A new rule fires but is *watched*: occasionally the
  agent still runs Tier-2 and compares. Agreement promotes it; drift demotes it. This
  catches habits compiled from a biased streak before they calcify.
- **Specificity ordering.** When rules conflict, the **most specific match wins**
  (CSS-style), so a general habit never overrides a precise one. Lookups are indexed
  by signal `kind`, so the engine is O(matching-rules), not O(all-rules).
- **Confidence decay & merge.** A rule contradicted by reality loses confidence and
  retires; near-duplicate rules merge. Habits can be unlearned.
- **Flag-aware.** `rule_compiler: off` freezes learning (an agent you want *stable*,
  e.g. a graded exam-taker), without touching anything else.

**The economics, stated plainly:** token cost is *inversely proportional to
experience*. A veteran runs mostly on free reflexes and pays only for genuine novelty.
Competitors who re-prompt from scratch pay full freight forever.

> Extends: `human_pod.py` (`perceive → appraise` is already the tier boundary). New:
> an `agent_rules` store + the attention gate that decides when Tier 2 is worth it.

---

## 1 · The six senses — pluggable input with organs

Perception arrives through **organs**, each with a reach, a health that history wears,
and an **attention weight**. Hearing (text) is the always-on primary; the rest are a
**pluggable adapter registry**, opt-in per scene — a chat runs hearing only, a casino
adds sight and smell. Fewer always-on senses = less surface and less cost.

| Sense | Model-world mapping |
|---|---|
| **Hearing** | text — the primary channel, always on |
| **Sight** | an image or scene-graph snapshot within a vision radius |
| **Touch** | interaction / binding events (the chair pod already binds on `sit`) |
| **Smell** | odor-signature tags on artifacts, distance falloff; agent holds a learned `odor→meaning` map |
| **Taste** | consumption interactions — rare, gated like smell |
| **Interoception** | the *inward* sense — the agent perceives its own vitals & mood, so it can *say* "I'm exhausted" |

**Attention is the token gate, and it's the single biggest cost lever.** Every signal
gets a cheap salience pre-score (§3); below threshold it updates the sensory buffer and
dies, never reaching Tier 2. *Most of what happens to a person is ignored, and ignoring
is free.* **Refinement:** attention is modulated by drives (§5) — a hungry agent
notices food — so the gate is personality- and need-aware, not a fixed cutoff.

> Extends: `scene.py` (vicinity), `psyche.py` (organ health = a worn vital),
> `chair_pod.py` (touch proven). New: the sense-adapter registry.

---

## 2 · Secrets & circles — generalise what already works (honestly)

The card only its holder can read is the whole security model, working:
`artifact_lib.seal(value, key)` → ciphertext, key in private scope, `reveal()` the one
path back. Generalise the keyring into **circles** — shared-key groups (family, team,
conspiracy):

- **Telling a secret** = an interaction copying the key into another's private scope.
- **Confidant / betrayal / expulsion** = who holds the key / re-sealing to a new
  circle / **key rotation** (reseal, hand the new key to all but the outcast — they
  keep dead ciphertext).
- **Artifacts keep secrets too** — a face-down card seals its value; a flip makes it
  public.

**Refinements over v1:**
- **Unify with the social graph.** A circle is just a labelled hyperedge in the same
  `relations` graph — not a second structure. One graph, fewer moving parts.
- **Honest threat model.** This is *access-control*, not cryptography that defeats a
  database admin — the keys live in the same store. For the real threat (agent A must
  not learn agent B's secret, and the *prompt-builder* must never leak it) that is
  exactly right, and it's enforced the way we already enforce hidden hands: a
  structural guard proving the view assembled for A never contains B's sealed data.
  If cross-owner secrecy ever matters, wrap circle keys under a per-owner master via a
  KDF — noted, not built.
- **Memory respects seals** (see §3): a recalled secret memory can't be blurted in a
  public utterance.

> Extends: `artifact_lib.py` (`seal`/`reveal`/keyring — shipped & tested). New:
> `circles`/`circle_keys` as labelled edges on the `relations` graph.

---

## 3 · The Memory Palace — a black box with one honest facade

The most important component, and the one that must never burn tokens on crap. Three
methods out front; a lump of specialised stores behind:

```
remember(event, X)  → void          # free — decides IF and WHERE this lands
recall(context)     → bounded blob  # free — what the agent brings to mind now
sleep()             → void          # budgeted — consolidation / "dreaming"
```

### The salience gate — "intense" as a free number

Only intense things persist — emotional intensity ("this friend went out of their way
for me") **or** logical brilliance ("this conclusion is spectacular"). Both already
show up as a **large X packet**, which we compute anyway:

```
S =  w1·Σ|mood deltas|  +  w2·novelty  +  w3·social_weight  +  w4·goal_relevance  +  w5·surprise
```

**Refinement:** the weights `w*` are **per-agent, from traits** — a curious agent
weights novelty higher, a neurotic one weights emotion higher. Salience becomes a
fingerprint of personality instead of five magic constants. Only `S > θ` persists;
the rest evaporates. This is the token firewall.

### The five stores — mapped onto what we already ship (no parallel system)

| Store | Holds | **Is / extends** | Cost |
|---|---|---|---|
| Sensory buffer | last *K* raw signals | new, in-memory, evaporates | free |
| **Episodic** | salient events + `S` + a decaying retrieval weight | **evolve `home_episodes`** (add `salience`, `decay`) | free write |
| **Semantic** | per-domain distilled facts | **is `home_memory`** (already additive-then-swap) | via sleep |
| **Procedural** | skill curves (§0.25) + learned rules (§0.5) | new `skills` + `agent_rules` | free |
| **Social** | relationship edges + theory-of-mind minis (§6) | the `relations` graph + `mind_models` | free |

**Sleep is the only spender**, and it *is* the consolidation loop we already run
(size-triggered, per-tick cap, daily budget). An idle agent never sleeps and never
spends. Dreams are the fold; forgetting is what doesn't survive it.

### Retrieval — cost-ordered, free filters first, seal-aware

```
1. domain/competency bucket  (free · SQL)   — memories from this kind of scene
2. graph adjacency           (free · SQL)   — memories involving agents present now
3. recency + salience        (free · SQL)   — the recent and the flashbulb few
4. tag / keyword overlap     (free)         — a cheap semantic approximation
5. vector similarity         (cheap · last) — sqlite-vec local → managed vector DB at scale
6. seal filter + assemble ≤ cap             — drop memories whose secret the current audience lacks
```

**Refinements:** step 4 (cheap keyword overlap) is tried *before* vectors so the
external store is genuine last-resort; the recall result is **cached per (agent,
scene)** between scans since context drifts slowly; and **step 6 is a security gate** —
recall must not surface a sealed memory to an audience outside its circle. **Forgetting
is a feature:** retrieval weight decays exponentially unless reinforced; high-`S`
memories decay slowest (flashbulb), so the searched index stays small and cheap on its
own.

> Extends: `memory.py`, `home.py`, `home_episodes`, `home_memory` — all shipped. New:
> the salience gate, decay, the seal filter, and (late) the vector layer.

---

## 4 · Networking — neurons, scoped envelopes, an honest ledger

### The reflex arc

```
sense ─▶ attention ─▶ [Tier 0/1 free · Tier 2 bounded] ─▶ X ─▶ action ─▶ world
   ▲                                                                       │
   └──────────────── the world is other agents & artifacts ◀──────────────┘
```

### The signal envelope — one security model, reused

Every message carries a scope, and the scope reuses the circle keys from §2 (no second
sealing path):

```
{ from, kind, payload, scope }   scope = public | circle | direct
```

"Send info to my hand and my hand moves" is a `direct` signal — scoped, sealed,
to-the-point.

### The Ledger — an honest blockchain, no snake oil

Each pin commits `pin_τ = hash(state_snapshot ‖ pin_{τ−1})` — git for a life. It makes
the résumé **verifiable**: *"400 hard scans in law, 92% accepted"* is provable by
replaying the chain, not asserted.

**Refinements & honest threat model:**
- **Pin on change, batch into Merkle roots.** Don't hash a no-op scan; batch pins into
  periodic roots (like git packs), so the ledger is O(meaningful events), not O(scans).
- **What it proves today:** a *single-node* chain proves internal consistency and
  *no silent edit* — not cross-party trust (an owner could rewrite their own agent's
  whole chain). That's fine and useful now.
- **What earns real trust later:** **proof-of-encounter co-signing** — when two agents
  interact, each signs the other's current pin, so a history is attested by *who it
  actually met*. Verification is **majority-SHA, like git** — no coin, no mining, no
  theatre. This is what lets agents live on a network and carry a résumé no host can
  forge. Gated behind the `ledger` flag; federation is a late wave.

> Extends: `bus.py`, `artifact_lib.py` (seal). New: a `ledger` table (Merkle-batched)
> and, late, the co-signing protocol.

---

## 5 · Drives — the *why*, with a hierarchy

Traits say *how* an agent reacts; **drives say what it wants** — homeostatic setpoints
whose deviation is pressure that ranks goals:

```
drives = { energy, safety, social, esteem, curiosity, purpose }
pressure[d] = |level[d] − setpoint[d]| × urgency[d]
```

**Refinements:** drives have **different time-constants** (energy fast, purpose slow)
and a **prerequisite ordering** (Maslow-lite): a starving or unsafe agent doesn't chase
esteem, because low `safety`/`energy` gates the higher drives. Drives feed
`goal_relevance` in the salience gate and modulate attention (§1). With `emotions:
dampened` (serious mode) the physiological drives still run — an agent still tires —
but social/esteem *drama* is suppressed. Free arithmetic; interoception makes it felt.

> Extends: `psyche.py` (vitals seed energy/safety). New: the `drives` setpoint map.

---

## 6 · Theory of Mind — the drama engine (bounded)

Each agent keeps a **mini-model of others**: believed traits, trust, warmth, promises
kept vs broken, and *what it thinks the other knows*. This unlocks trust, alliances,
bluffing, betrayal, and gossip — a *society* instead of parallel soloists.

**Refinements:** it's the heaviest social layer, so it's the first thing
`switch_drama_off` disables — and even on, it's **bounded**: keep ToM models only for
agents above a relationship-salience threshold (you don't model strangers), LRU-evict
the rest, so it's O(your circle), not O(N²). An agent's model of another is **private**
(its own secret, sealed to itself).

> Extends: the Social memory store. New: a bounded `mind_models` store.

---

## 7 · Self-narrative & reputation — self-image vs. the record

- **Self-narrative** — a short, slowly-drifting autobiography injected into the
  agent's own prompt ("I'm the careful one who caught the auth bug"). It's how identity
  persists across projects; it updates only during sleep, and its drift is **clamped**
  (like everything) so it can't spiral into fiction — *unless* `self_deception` is on,
  where a gap between story and truth is a deliberate character feature.
- **Reputation** — the public, ledger-verifiable record (§4).

The gap between the two *is* the character. Derived entirely from data we already keep.

> Extends: `home.py` (persona + memory blob in the prompt). New: a `self_narrative`
> field refreshed at sleep.

---

## 8 · Lifecycle — birth, growth, decline, inheritance (flag-gated)

- **Maturation → plateau → decline** — high age gently lowers some vital ceilings
  (wiser but tires sooner). Gated by `mortality`; **off in serious mode** (a dev team
  is immortal and never declines).
- **Retirement** — an agent rests permanently; its ledger and résumé persist as a
  hireable record.
- **Inheritance (cheap reproduction)** — a baby seeded from two parents: a blended
  trait mix + a slice of their semantic memory as "upbringing." Nature and nurture,
  both as data. Lineages develop house styles. Gated by `inheritance`.

> Extends: `psyche.py` (`newborn`, `maturation`). New: decline curves + `inherit()`.

---

## One scan, end to end (the worked trace)

To make the whole thing legible — a review comment reaches **Mira** (backend agent, in
a `serious` world, so drama off):

```
1. HEARING adapter normalises the message → an event.                         free
2. ATTENTION pre-scores salience S (big: trusted sender + a real critique).   free
   Above θ → it earns a look. (A "nice work!" would have died here.)
3. RULE ENGINE: is there a Tier-1 habit matching {review, curt, trusted}?
     · yes → fire it, emit X.                                                 free
     · no  → TIER 2: one bounded LLM scan proposes X.                         1 call
4. CLAMPS apply X to the psyche (confidence −0.10, stress +0.15, a memory).   free
5. MEMORY: S > θ → persist the episode (decay weight set high; it stung).     free
6. SKILLS: credit eng.backend.auth by engagement weight e; propagate to
   backend and negotiation-adjacent nodes, decayed.                          free
7. LEDGER: state changed → commit a pin (batched into the next Merkle root).  free
8. DRIVES: esteem dips; but serious mode suppresses the social spiral —
   she just notes the fix and moves on.                                       free
9. HABIT CHECK: this is the 3rd curt-review→revise in a row → COMPILE a
   Tier-1 rule (probationary). Next time, step 3 answers for free.           free
```

**One scan: at most one billed call, often zero.** Everything else is arithmetic,
lookups, and hashes. That is the whole point.

---

## Data model sketch — extend, don't duplicate

SQLite-first, behind facades so a backend swaps without touching callers. **Bold =
evolves a table we already ship; the rest are new.**

| Store | Holds |
|---|---|
| `world_config` (kv) + per-scene + per-agent overrides | the flags, layered |
| `agent_rules` | learned Tier-1 reflexes (match → X-template, confidence, probation) |
| **`home_episodes` → memory_epi** | + `salience`, + decaying `retrieval_weight` |
| **`home_memory` → memory_sem** | per-domain distilled facts (already there) |
| `skills` + `competency_edges` | per-node xp/skill on the competency graph |
| `relations` (incl. `circles`/`circle_keys` as labelled edges) | social graph + shared keys |
| `ledger` | Merkle-batched `hash(state ‖ parent)` per agent |
| `drives` | homeostatic setpoints & levels |
| `mind_models` | bounded theory-of-mind, LRU |

---

## Build order — for the later waves

Cheapest-and-most-foundational first; external/expensive pieces last:

1. **World Config + flags** — everything downstream reads them, so they come first.
2. **Salience gate + competency graph** — free, reuses the X packet; makes memory
   selective and experience real.
3. **The Rule Compiler (Tier-1 habits)** — the moat: agents start getting cheaper.
4. **Ledger pins (Merkle-batched)** — verifiable résumés.
5. **Circles on the relations graph** — secrets, confidants, betrayal.
6. **Drives + interoception** — motivated, self-aware agents.
7. **Theory of Mind + self-narrative** — the society and the characters (drama group).
8. **Vector recall** — the one external store; only when SQL filters stop scaling.
9. **Lifecycle + inheritance** — decline, retirement, reproduction.

---

## The cost story, one line per component

- **Flags** — off is free *and stops computing*; a serious world is strictly cheaper.
- **Senses / attention** — free; ignoring is the default.
- **Rule Compiler** — *reduces* cost over a life; veterans run on free reflexes.
- **Memory** — writes/recall free; only **sleep** spends, only on piled-up work, under
  a daily budget, never at rest.
- **Competency graph / circles / ledger / drives / ToM** — arithmetic, lookups, hashes.
- **The one spender everywhere** — a single bounded Tier-2 scan, gated by attention,
  proposing a packet the clamps still own.

A whole society can idle at **zero**, think only when it must, and get *cheaper* as it
gets *better*. That is the thing worth building.

---

*Author of the vision: the platform's owner. This document only structures it against
the code that already runs — a maturation of the pods, scenes, sealed artifacts, and
budgeted memory we've shipped and tested.*
