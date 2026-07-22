# The Studio, sprint 2 — Scenes, Artifacts-as-code, and a Manager who walks the room

*The plan for turning the Studio from a room of agents into a small world where
things happen. The acceptance test is the one the owner named: five agents seated
at a table in a casino scene start a poker match on their own, a manager on the
canvas the owner can talk to drives it, and the cards are code.*

---

## The one idea that makes all of this coherent

The owner's sharpest line is the design: **an artifact is code, not an AI.** Hold
onto that and everything else falls into place, because the platform already has
this shape and has never named it:

> A **project** is a scene. A **task** is an artifact — a piece of work with a
> deterministic effect (its verification runs, its files land) that shapes what the
> agent working it produces. The **manager** already walks that scene deciding who
> does what. The poker table is the same machine with a different scene loaded.

So this is not a toy bolted onto the side. It is the general substrate the
project/team/manager system is a special case of — and building it makes the whole
product one model instead of two. The casino is how we prove the substrate; a
software team is what we ship it for.

**Three nouns, and what each is:**

| | What it is | Costs tokens? |
|---|---|---|
| **Agent** | A character with a persona, a model, memory, and *private* state. Already built. | Only when it speaks/decides |
| **Scene** | A setting with rules and a goal, that shapes what agents do while in it. Data + deterministic rules. | Never |
| **Artifact** | A piece of **code** an agent interacts with, that runs and changes the agent's state or shapes its next utterance. A card, a contract, a clue. | Never (it is code) |

The manager is an agent whose scene-role is *orchestrator*. Nothing special in the
data — special only in what the scene lets it do.

---

## Artifacts are the crux — get these right and the rest is wiring

An artifact is a small, sandboxed, deterministic program with three parts:

1. **State** — its data. A card's rank and suit; a contract's clauses; a clue's text.
2. **Visibility** — who can see it, and how much. A card can be **face-down** (only
   its holder sees the value; everyone sees a back), face-up (all see it), or
   private (only the holder knows it exists). This is where "an agent can hide what
   it doesn't want revealed" becomes a real, testable property, not a promise.
3. **Effect** — a pure function `(artifact, agent_state) -> agent_state'` that runs
   when an agent *interacts* with it (sees, holds, flips). **This is the owner's
   example exactly:** a card seen by agent 1 is added to agent 1's private hand;
   when agent 1 has seen five cards, the effect *collates* them — deterministically,
   the cards' own code combines into one output (a poker hand rank) that the agent
   then *speaks about*. The collation is code; the speaking is the one place a token
   is spent.

The critical discipline, and the reason this can scale: **effects are free.**
Flipping a card, dealing, adding to a hand, computing a hand rank, checking who can
see what — all deterministic code, zero model calls. A model is touched only when
an agent has to *say or decide* something, and that is turn-based, one speaker at a
time, hard-capped. A five-player poker match is not five agents chattering in
parallel; it is a deterministic dealer (code) advancing a turn order, and at most
one agent thinking at a time about a hand the code already computed for it.

Artifacts are code, so they need a safe way to run. **They do not run arbitrary
Python.** An artifact is one of a small set of *typed* effects the platform ships
(a `card`, a `deck`, a `pot`, a `contract`), each a reviewed Python function
selected by type — the same way `deploy.detect` dispatches on a known kind rather
than executing whatever a repo contains. "Later, a custom artifact compiled by us"
becomes "add a new reviewed effect type," never "exec a string." That keeps the
analogy's power without opening an RCE in a multi-tenant system — which, given the
credential leak we just closed, is exactly the kind of door to keep shut.

---

## The token budget — the same discipline, extended

The owner's first rule holds without exception: **a scene at rest costs zero, and
the machinery is deterministic; only utterances bill.**

| Operation | Free or bills | Bound |
|---|---|---|
| Loading a scene, seating agents | free | — |
| Dealing / flipping / any artifact effect | **free — it is code** | — |
| The dealer advancing the turn order | free (deterministic, like the scheduler) | — |
| Computing a hand rank from seen cards | **free — collation is code** | — |
| An agent taking its turn (deciding, speaking) | bills — **one** bounded call | `max_tokens`, one at a time |
| The manager briefing a player | bills — **one** bounded call per player visited | per-scene cap |
| A whole match | bills O(turns), never O(agents²) | a hard per-scene token budget |

The N² trap — five agents each reacting to each other's every move — is forbidden
by construction: the *dealer is code* and imposes a turn order, so only the agent
whose turn it is thinks, about state the code already prepared. "Agents talking to
each other" is mediated by the scene's rules, never a free-for-all. Every scene
carries a token budget like the daily Studio budget; when it is spent the match
pauses rather than quietly running up a bill.

---

## The manager who walks the room

The owner wants to talk to a manager on the canvas, tell it a goal, and watch it go
brief each player. That is two things:

- **A conversation surface** — the manager is an agent; talking to it is the same
  turn-based, bounded exchange as talking to any resident. It holds the user's goal
  as its brief.
- **An orchestration verb** — "make them play." The manager, given the goal and the
  scene, produces a deterministic plan (seat order, who is briefed with what) and
  then *executes it as an animation*: it visibly moves to each player (the same
  positioned-node movement the round table already animates), and each visit is one
  bounded briefing call. The walk is free; the briefings are the spend, one per
  player, capped.

This is the project manager generalised. Today it plans a DAG and reviews; here it
plans a seating and briefs. Same agent, same "you can talk to it," a scene-specific
set of things it may do.

---

## Data model (extends what exists)

Two new tables, and a link — mirroring how `home_agents` extended `agents`:

- **`scenes`** — id, owner_id, kind (`poker` | `project` | `debate` | …), goal
  (the user's brief to the manager), status, token_budget, tokens_spent, a `layout`
  blob (positions), created_at. A scene is owned, private, and carries its own
  budget.
- **`scene_agents`** — which agents are in a scene, their seat/role (`player` |
  `manager` | `dealer`), and their **private scene-state blob** (a poker hand). This
  is where "an agent's secret" lives: scene-state is never sent to another agent's
  prompt and never returned by another agent's view — the isolation test is written
  first, exactly as the credential-isolation guard was.
- **`artifacts`** — id, scene_id, type (`card` | `deck` | `pot` | …), state (JSON),
  visibility (`public` | `held:<agent>` | `facedown:<agent>` | `hidden`), and a log
  of who has interacted. No code column — the effect is selected by `type` from the
  shipped, reviewed set.
- **`scene_events`** — the transcript: deals, flips, utterances, the manager's
  visits. Free to append, drives both the animation and the audit.

Nothing here touches the project/team tables. A scene of kind `project` can later
*be* a project — the substrate unifying the two is a follow-up, not this sprint.

---

## The private-state guarantee, made testable

The owner asked us to *establish* that a secret is not revealed. It becomes a
first-class, tested invariant:

- An agent's scene-state (its hand) is stored only on its own `scene_agents` row.
- The prompt built for agent A never includes agent B's private state or a
  face-down artifact A cannot see — assembled by a single function whose only job
  is "what may this agent know," with the test suite proving a hidden card A holds
  is absent from B's prompt and B's `/api/scene/{id}` view.
- A structural guard reads the prompt-builder source and asserts it reads private
  state only for the agent being prompted — the same shape as the "no server
  credential in the key path" guard, so a future edit cannot quietly widen it.

---

## The sprint — ordered, with the poker match as the acceptance test

**Foundation**
1. `scenes` + `scene_agents` + `artifacts` + `scene_events` tables + accessors; the
   `scene_agent.private_state` blob and its owner-scoped read.
2. `artifacts.py` — the typed, reviewed effect registry (`card`, `deck`, `pot`),
   each a pure `(artifact, state) -> state`; visibility rules; the free `collate`
   that combines seen cards into a hand rank. No arbitrary code, ever.
3. The **private-knowledge builder** + its isolation tests, written before anything
   speaks — a secret must be un-leakable before an agent can hold one.

**The dealer and the turn engine (free)**
4. `scene.py` — load a scene, seat agents, a deterministic dealer that deals and
   advances a turn order with zero model calls; per-scene token budget like the
   daily one.
5. Wire the free background/step engine so a scene advances turn by turn, spending
   only on the acting agent's decision.

**The manager and the spend**
6. The agent-turn call: one bounded utterance/decision from the acting agent, over
   only what it may see, on its own model.
7. The manager's orchestration verb: plan the seating, then walk-and-brief — one
   bounded call per player, animated.
8. The conversation surface: talk to the manager (and any agent) on the canvas,
   turn-based and bounded — reusing the round-table speech playback.

**The world, on screen**
9. Scenes on the canvas: right-click → *Set a scene* (the menu already signposts
   it), drag agents onto it, a table for a poker scene. Cards as artifacts you can
   see flip; a face-down card shows a back.
10. The manager's walk, the deal, the reveal — animated with the existing
    positioned-node + speech-queue machinery.

**Proof**
11. The acceptance test, end to end and offline: five seeded agents, a poker scene,
    the manager told "play a hand"; assert the match completes, exactly one agent
    thinks per turn, the total model calls are O(turns) and under the scene budget,
    each player's hand stays private, and the winner is decided by the *code* (the
    collated rank), not by a model's say-so.

---

## What I would build first, and what I would resist

Build the **artifact registry and the private-knowledge builder first** — they are
the two ideas that are genuinely new and genuinely dangerous to get wrong (an
artifact that runs arbitrary code, or a secret that leaks). Everything else is the
turn engine and the animation, both of which reuse machinery that already exists.

Resist two temptations: letting artifacts execute arbitrary code (ship typed
effects, add types by review), and letting agents react to each other freely (the
dealer is code; it imposes turns). Both are where the token bill and the security
hole would come from, and both are the owner's own constraints turned into
architecture.
