# Patentability & prior-art assessment — the Living World architecture

**Read this first.** I am not a lawyer and this is not legal advice. Patentability is
a legal judgement that depends on the exact claims you draft, your jurisdiction, and a
professional novelty search. Nothing here should be relied on for a filing decision;
its job is to give you an honest, technically-informed starting picture so you spend a
patent attorney's time (and your money) well.

The two questions you asked, answered plainly, then supported:

1. **"Can I file a patent on this idea?"** — You cannot patent *the idea/theory as
   such*. You may be able to patent one or more **specific, novel, non-obvious
   technical mechanisms** inside it — but not the vision. Realistically, the broad
   architecture as written would not survive, while a narrow, concrete sub-mechanism
   *might*. See §2–§4.
2. **"Is it copied?"** — Not in the plagiarism sense: you clearly derived it yourself.
   But large parts of it **substantially overlap well-established prior art**, some of
   it decades old and some very recent. Independent invention does not create patent
   rights when prior art already exists. See §1. This is the harder truth of the two.

---

## 1. Prior art — what already exists that overlaps

This is the most important section, because novelty is judged against *everything
published anywhere before your filing date*, regardless of whether you knew of it.
Your architecture is a synthesis of several mature research lines. Each row is a real,
findable body of work a patent examiner (or an opponent) would cite.

| Your element | Established prior art that covers it |
|---|---|
| Agents with an **inner emotional state that changes from events** | **Affective computing** — Rosalind Picard, MIT Media Lab, book *Affective Computing* (1997). A whole field. |
| **Appraising an event for its meaning → emotional consequences**, gated by the agent's goals/history | **Appraisal theory**, and its computational forms: the **OCC model** (Ortony, Clore & Collins, *The Cognitive Structure of Emotions*, 1988) and **EMA** (Gratch & Marsella, 2004) — emotion as the output of appraising an event against what the agent cares about. This is almost exactly your "a scold is interpreted, then lands in buckets if it triggers something in H." |
| **Buckets/dimensions whose quantity weights behaviour** | **PAD model** (Mehrabian — Pleasure/Arousal/Dominance dimensions); and, in games, **The Sims** (Maxis, 2000) — visible *need/mood meters* that fill and drain and drive autonomous action. Your "buckets" are conceptually the Sims' needs plus OCC's emotion variables. |
| **Objects that broadcast to nearby agents and change the agent's state** | **Smart Objects** — Kallmann & Thalmann, "Modeling Objects for Interaction Tasks" (1999); the mechanism behind The Sims, where an object advertises interactions to agents in range and alters their state. This is your "artifact is a pod that interacts with an H in its vicinity." |
| **Sensory perception with distance thresholds (sight/hearing radius)** | Standard **game-AI perception systems** for 20+ years (sight cones, hearing radii; e.g. *Thief*, *Metal Gear Solid*, *F.E.A.R.*). Embodied virtual-human sensing: USC ICT **Virtual Humans** (Gratch, Marsella). |
| **A body/organs whose capability degrades with history** | Deep simulation games (**Dwarf Fortress** body/tissue/health simulation); biological-aging models. Less common in AI agents, but not novel as a concept. |
| **LLM-driven agents in a simulated world, with memory that shapes future behaviour, producing emergent social behaviour** | **"Generative Agents: Interactive Simulacra of Human Behavior"** — Park, O'Brien, Cai, Morris, Liang, Bernstein (Stanford/Google, 2023). 25 agents in a Sims-like town, each with a memory stream, reflection and planning. **This is the single closest piece of prior art to your overall vision** and it is recent, famous, and code-available. |
| **An agent whose parameters/self change from experience** (`output → B'`) | **Reinforcement learning** in its entirety (input → action → reward → update to the agent), and **memory-augmented LLM agents** (Voyager, Reflexion, generative-agent memory streams). The "the run changes the runner" idea is the foundational loop of RL. |
| **LLM personas with persistent memory and personality** | **Character.AI**, **Replika**, and a large 2023–2025 literature on persona agents and agent memory. |
| **Hidden state that leaks through the arrangement of objects** | Game state-visibility / information-hiding systems; the specific "glass table reveals the card" is a fresh *illustration*, but "visibility is a function of object arrangement" is ordinary simulation logic. |

**Honest conclusion for §1:** the *overall system* — LLM-driven synthetic humans with
appraised emotions in fillable buckets, embodied sensing by distance thresholds,
stateful objects that affect nearby agents, all learning by being changed through
experience — is a **synthesis of known art**, and its closest single antecedent
(Stanford Generative Agents, 2023) predates this write-up. As a whole, it is very
unlikely to be considered novel. That is not a criticism of the thinking — arriving
here independently is a real signal — but patents reward *being first to file on
something new*, not *thinking of it yourself*.

---

## 2. You cannot patent an idea — only a specific invention

A patent does not protect a theory, a discovery, a business goal, or "a system that
simulates human emotion." It protects **claims**: precise, technical, structural
descriptions of a method or apparatus. Three independent bars must all be cleared:

- **Eligibility** (is this the *kind* of thing that can be patented at all?)
- **Novelty** (is it new versus all prior art? — §1 is the problem here)
- **Non-obviousness / inventive step** (would it be obvious to a skilled engineer
  combining the known art? — §1 is also the problem here)

The vision fails all three *as stated*. A narrow mechanism might pass all three. The
work of patenting is entirely about finding and drafting that narrow mechanism.

---

## 3. Eligibility — the specific hazard for software like this

In the **US** (*Alice Corp. v. CLS Bank*, 2014), an **abstract idea** — including
mental processes and "methods of organizing human behaviour" — implemented on a
generic computer is **not eligible**, unless the claim adds an **inventive concept**
that is a concrete **technical improvement**. "Simulate a human's emotional reaction
to a message using an AI model and store it in variables" reads squarely as an
abstract mental process on a computer, and would likely be rejected under §101 unless
tied to a specific, non-generic technical mechanism that improves how a machine works.

In **Europe** (EPC Art. 52), you need a **technical effect** beyond the mere
computation; "modelling human emotion" tends to be treated as non-technical subject
matter unless the invention solves a concrete technical problem in a technical way.

The practical implication: a claim survives eligibility only if it is about *how the
machine does something better/differently in a technical sense* — not about *what
human-like thing is being simulated*.

---

## 4. Where a real, narrow patent might actually live

If you want to pursue this, do **not** try to claim the world. Look for a specific,
non-obvious *technical* mechanism that (a) you can describe in engineering detail, (b)
is not in §1, and (c) improves the machine. Candidates to explore with an attorney —
each would still need a novelty search:

- A **specific data structure and update rule** for the buckets that provably bounds
  runaway feedback and conserves identity across many self-rewrites (the stability
  problem in LIVING_WORLD §5) — if your solution is a genuine technical one, *that*
  mechanism, not the buckets, could be the invention.
- A **specific method for turning appraised meaning into bucket deltas gated by a
  learned personality embedding** — if the gating function is a concrete, novel
  technique rather than "ask an LLM."
- A **specific architecture for the perception pipeline** — how vicinity signals are
  computed, attenuated by organ-health-from-history, and routed — if it is a concrete
  systems contribution (e.g. a novel efficient event-propagation scheme).
- A **cost-control mechanism**: a specific method for deciding *when* to spend an
  expensive appraisal versus a cheap deterministic one, that measurably reduces
  compute for a given behavioural fidelity. Efficiency improvements are often the most
  defensible software patents because they are unambiguously *technical*.

Note the pattern: the defensible inventions are the **hard engineering answers to the
black boxes**, not the vision that names them.

---

## 5. What to actually do (in order)

1. **Do not publicly disclose the specifics you might patent.** In the US you have a
   12-month grace period after your own public disclosure; most of the rest of the
   world (Europe, China) is **absolute novelty** — any public disclosure before filing
   destroys the right. Keep unfiled mechanisms confidential. *(This repo is private;
   keep it that way for anything you may file.)*
2. **Build one black box into a concrete mechanism first.** You cannot patent
   "buckets"; you might patent *your specific, working bucket-update algorithm*. The
   invention has to exist as engineering before it can be claimed.
3. **Commission a professional prior-art / novelty search** on that specific
   mechanism (a patent attorney or a search firm; a few hundred to low-thousands of
   dollars). This is the step that tells you the truth §1 only gestures at.
4. **Consider a provisional patent application (US)** once you have a specific
   mechanism: it is cheap, gives you a filing date and "patent pending," and buys 12
   months to develop and decide on a full application. It does **not** require final
   claims.
5. **Talk to a registered patent attorney** with software/AI experience before any of
   the above becomes a filing. This memo is to make that conversation efficient, not
   to replace it.

## 6. Protection you already have, without a patent

- **Copyright** is automatic on the code and on these documents — it protects your
  *specific expression* (this implementation, this text), though **not the idea**.
- **Trade secret** protects the unpublished mechanism for as long as you keep it
  secret — often the right choice for an algorithm that is hard to reverse-engineer
  from a running product.
- **Defensive publication** — if you do *not* want to patent but want to stop anyone
  else patenting it, publishing it dated (which this repo, and a timestamped
  disclosure, begins to do) creates prior art against later filers.

---

### Bottom line

- **Patent the idea? No** — ideas and theories are not patentable; only specific,
  novel, non-obvious, technical mechanisms are.
- **Is the vision novel enough to patent as a whole? Almost certainly not** — it
  synthesises affective computing, appraisal/OCC, The Sims' needs and smart-objects,
  game-AI perception, RL's learning loop, and 2023's Generative Agents, all of which
  predate it.
- **Is anything here patentable? Possibly** — but only the *hard, specific engineering
  answers* to the open problems in LIVING_WORLD §5, and only after a real novelty
  search and an attorney. The value of your work right now is the coherent *synthesis
  and direction*, which is worth building — it is just not, as a whole, a patent.

*Not legal advice. Consult a registered patent attorney before making any filing
decision.*
