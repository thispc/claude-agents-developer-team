# The Living World — an architecture for self-modifying agents in a sensed environment

*The owner's theory, captured faithfully. This is the foundation, however weak the
black box is today. Nothing here is casino-shaped; the poker table was only ever one
throwaway instance of the real atom below.*

---

## 0. The one thesis

> Everything in the world — a person, a card, a rose, a table — is **code running in
> a pod**. Every such pod is the same shape: it **takes input from its surroundings,
> processes it, and produces output — and the output rewrites the pod itself.** That
> last clause is the whole point. A pod is not a function; it is a function that is
> changed by having been run. Experience alters the machine.

Formally, every entity is a loop:

```
        surroundings
             │  input
             ▼
     ┌───────────────┐
     │   pod (B)      │   ← B is the entity's own framework + state, in a repo
     │  input → out   │
     └───────┬───────┘
             │  output
             ▼          ┌──────────────┐
        the world  ◄────┤ and B := B'  │   the output mutates B into B'
                        └──────────────┘
```

`input → B → output`, where `output` also `→ B'`. B is intelligent *about that
experience* because the experience left a mark on B.

---

## 1. Entity types

There is one primitive (the pod-with-a-self-rewriting-framework) and, so far, two
kinds of it:

| Type | What it is | Example |
|---|---|---|
| **H** — a human | A pod running the *human framework*: perception, an inner life of layers/buckets, and behaviour. Born empty (a "baby H") and grown by experience. | a person at the table |
| **A** — an artifact | A pod running an *object framework*: a state that changes when something interacts with it, and rules about who can perceive that state. **An artifact is code, not a mind.** | a card, a ball, a rose, a glass table |

The type list is open — the theory expects **other types we may need** (a place, a
sound, a rule, a weather). The primitive does not care which type it is running; H
and A differ only in *which framework* the pod runs.

An LLM — our best available model of a human interpretation — can be run **inside**
any framework, H or A, to supply the layers of human meaning the raw code cannot.
The LLM is a component of a pod, not the pod itself.

---

## 2. H — the anatomy of a synthetic human

### 2.1 Birth: the empty framework

An H begins as a **baby H**: an empty human framework, stored in a repository, with
the capacity to receive input from surrounding artifacts but with none of the
content a lived life deposits. Everything that makes this particular H *someone* is
written into that repo over time by the loop in §0. Identity is not configured; it
**accretes**.

### 2.2 The inner life: layers and buckets

Inside H is a set of **layers**, each holding a **bucket** — a quantity. A bucket is
a dimension of the inner state: *insecurity*, *hope*, and however many more the model
needs. **The quantity in a bucket determines how strongly that element shapes the
output.** A near-empty *hope* bucket and a full *insecurity* bucket produce a
different person, given the same input, than the reverse.

Buckets **fill and drain**. They are not set; they move, as a consequence of what
happens to H.

### 2.3 Appraisal: how an event becomes a change in the buckets

This is the mechanism that makes H feel alive. When an event reaches H — a message,
a scolding, a kindness — it is **not applied literally**. It is:

1. **Interpreted through human perception** — run through our best AI model to ask:
   *what would this do to a person?* The event is understood for its **meaning**, not
   its surface text.
2. **Decomposed into consequences** — the meaning is broken into what it does to the
   inner life. A scolding is not "−1 mood"; it is a set of effects.
3. **Routed into buckets, gated by history and personality** — a consequence lands in
   a bucket **only if the meaning triggers something already in H.** A scold that
   touches a part of this H's personality, or something from its past, **fills the
   insecurity bucket**; the same words to a different H, with a different history,
   might not. The same event might simultaneously **drain the hope bucket**.

So the effect of an event is a function of the event's *meaning* × this H's
*accumulated self*. That is why two agents, scolded identically, are wounded
differently — and why the wound is *earned*, traceable to what was already in the
repo.

### 2.4 The body and the senses

H is embodied. Perception is not free-floating; it comes through **sense organs**,
each with:

- a **vicinity threshold** — a distance at which a signal can reach that sense. A
  human can *see* something far, *touch* something only when close, *hear* within a
  radius, *smell* within another. Each artifact carries a **vicinity variable** —
  how near it is — and when it crosses a sense's threshold, a signal fires.
- **organ health, monitored over history** — the nose's ability to smell is a
  function of *years of previous activity that affects smell*. The body is not a
  fixed spec; it too is written by the life lived. A sense can dull.

Concretely: a rose's vicinity crosses H's smell threshold → a signal is sent to H's
**nose** → the nose's current health (derived from H's history) modulates the signal
→ the modulated smell becomes an input, appraised as in §2.3.

---

## 3. A — the anatomy of an artifact

An artifact is **also** a pod running a framework — simpler than H, but the same
shape: input → change of state → output, with the change persisting.

- **State that changes on interaction.** A card's framework changes its own state
  when something acts on it. When a dealer **flips** the card, the card's running
  framework changes *on the fly* so that its face is no longer perceivable.
- **Perception rules — who can see what, and leaks.** The flipped card is hidden to
  everyone at the table by default. But visibility is a property of the *arrangement
  of artifacts*, not a global truth: if the table it rests on is a **glass table**,
  then an H who *chooses to look under the table* perceives the card's face. The
  hidden state can **leak** through another artifact. Secrecy is emergent from the
  physics of the objects present, not declared.

Artifacts can run LLMs too, where an object needs a layer of interpretation (what a
letter *says*, what a photograph *means*).

---

## 4. The world and the loop

The world is a collection of H and A pods, plus the **surroundings** that connect
them — positions, vicinities, arrangements. Nothing is scripted. The world advances
by the same atom everywhere:

1. Artifacts and other Hs in the surroundings emit signals.
2. Each signal that crosses a sense threshold reaches an H's organ, is modulated by
   that organ's health, and enters as input.
3. The input is appraised for meaning against the H's accumulated self; buckets fill
   and drain.
4. The H acts; its action is a new signal into the surroundings, and may change an
   artifact's state.
5. **Every step leaves a mark**: the H's repo is rewritten, the artifact's state
   persists, the organ's history grows.

Self-learning is not a separate subsystem. It is what the loop *is*: a pod that is
changed by running is, over enough turns, learning.

---

## 5. What is specified, and what is still black box

Held honestly, because a foundation that hides its gaps is not a foundation.

**Specified (the theory commits to these):**
- The universal atom: `input → pod → output`, output mutates the pod.
- Two entity types, H and A, both pods; the type set is open.
- H's inner life as fillable/drainable buckets whose quantities weight output.
- Appraisal by meaning, gated by accumulated history/personality.
- Embodiment: sense organs, vicinity thresholds, organ health from history.
- Artifacts as stateful pods with emergent, leakable perception rules.
- LLMs as components inside frameworks, supplying human-meaning layers.

**Still black box (named, not yet answered):**
- The **exact representation** of a bucket, and the function from "appraised meaning"
  to "bucket deltas." How much does a scold drain hope? By what rule?
- How buckets **combine** into an output (a weighting? a competition? a learned
  policy?).
- The **repository format** of an H — what precisely is stored, and how a rewrite is
  applied without the self dissolving or ossifying.
- How **organ health** is computed from history, and the catalogue of senses.
- The **surroundings model** — coordinates, vicinity math, how signals propagate and
  attenuate.
- **Stability**: what stops runaway feedback (an insecurity bucket that fills itself
  forever), and what conserves identity across many rewrites.
- **Cost**: appraising every signal with a top model is expensive; the loop needs a
  free/cheap floor and a spend budget, exactly as the current platform already draws
  that line.

These are the research programme, not oversights. The point of this document is to
fix the *shape* so the black boxes can be attacked one at a time.

---

## 6. Glossary

- **Pod** — the unit of execution; one running entity.
- **B** — a pod's own framework-plus-state; the thing that gets rewritten.
- **H** — a synthetic human: a pod running the human framework.
- **A** — an artifact: a pod running an object framework; code, not a mind.
- **Baby H** — a newly born H, an empty human framework in a fresh repo.
- **Bucket / layer** — a quantity representing one dimension of an H's inner state
  (insecurity, hope, …); its amount weights its influence on output.
- **Appraisal** — interpreting an event for its *meaning* and its consequences for
  the inner life, gated by the H's accumulated self.
- **Vicinity** — how near an artifact is; compared against a sense's threshold to
  decide whether a signal reaches an organ.
- **Leak** — a hidden artifact state becoming perceivable through another artifact
  (the glass table).
- **The loop / the atom** — `input → pod → output`, where the output also rewrites
  the pod. The single mechanism the whole world runs on.

---

*Author of the theory: the platform's owner. This document only structures and
records it; every mechanism above is the owner's, set down so the weak parts can be
made strong deliberately rather than by accident.*
