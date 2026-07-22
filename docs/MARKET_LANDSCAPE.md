# Market landscape — who already builds "a world of living agents"

*Answering the owner's question directly: is there a product that does this? The
honest answer is that the space is crowded and hot, not empty. This maps the real
players so a differentiation and money strategy can be built on fact, not on the
assumption of open territory. Details are as of early 2026 and should be re-verified;
company status changes fast.*

---

## The uncomfortable headline

Every major component of the Living World vision — LLM agents with inner lives, in a
sensed world, that grow, form societies, and reproduce/evolve — **already ships or
has shipped as research, open source, or a funded product.** Not one unified product
identical to the full vision, but every piece has a credible prior mover. "I haven't
seen it" is not the same as "it doesn't exist," and here it mostly does.

---

## 1. LLM agents living in a simulated society

| Project | What it is | Why it matters to you |
|---|---|---|
| **Stanford "Generative Agents" / Smallville** (Park et al., 2023) | 25 LLM agents in a Sims-like town with memory, reflection, planning; emergent social behaviour (they threw a party, spread news). | The canonical prior work for your whole vision. Famous, peer-reviewed, code public. |
| **AI Town** (a16z, 2023, open source) | A deployable, hackable generative-agents town anyone can run. | Your "world of agents interacting" as a free repo people already fork. |
| **Altera — Project Sid** (2024) | 1,000+ autonomous AI agents in Minecraft that formed an economy, government, culture, even religion. Venture-funded. | This is "mock a humanity" at scale, already demonstrated and funded. |
| **DeepMind Melting Pot** | A multi-agent social-simulation benchmark — cooperation, competition, social dilemmas. | Establishes agent-society simulation as a mature research area. |
| **Fable — SHOW-1 / "The Simulation"** (2023) | Generated a watchable simulated South Park episode from agent simulation. | Commercial entertainment use of agent worlds. |

## 2. Life simulation as a shipped game (your "grow, reproduce, model of humanity")

| Product | What it is | Overlap |
|---|---|---|
| **The Sims** (Maxis, 2000–present, massive commercial franchise) | Simulated people with needs, moods, relationships, careers, reproduction, generations. | The commercial "model of human life" already exists and sells enormously. |
| **RimWorld** (2018) | Colonists with needs, moods, *mental breaks*, traumatic memories, relationships, pregnancy and children, personality traits that colour reactions. | Strikingly close to your **buckets + appraisal**: events are interpreted against a colonist's traits and history and change their mental state. |
| **Dwarf Fortress** (decades) | Deep world/history simulation across generations, body/health simulation, emergent stories. | Your "world modelled as closely as possible," minus LLMs. |

## 3. AI characters as a product for games (the commercial "agent engine")

| Company | What it sells |
|---|---|
| **Inworld AI** | A well-funded engine for LLM-driven NPCs with personality, memory, emotion, goals — sold to game studios. Directly the "agents for games" business. |
| **Convai** | Conversational AI NPCs for games and virtual worlds. |
| **NVIDIA ACE** | Digital-human agents (speech, animation, LLM brain) for games. |
| **Character.AI / Replika** | Consumer LLM personas with persistent personality and memory (tens of millions of users). |

## 4. "Agents from actual people" (your digital-clone angle)

| Company | What it does |
|---|---|
| **Delphi.ai, Personal.ai** | Build an AI clone of a real person from their content; the clone talks and acts like them. |
| **HeyGen / Synthesia / digital-avatar startups** | Photoreal digital twins of real people. |
| A wave of 2024–2025 "**digital twin of you**" startups | The exact "agents from real people" concept, monetised. |

## 5. Artificial life — the "reproduce and evolve" part, done since the 1990s

| Work | What it is |
|---|---|
| **Tierra** (Tom Ray, 1991), **Avida**, **Polyworld** (Yaeger) | Digital organisms that self-replicate, mutate, compete, and evolve — open-ended evolution. |
| Evolutionary / genetic multi-agent sims | "Agents that grow together and reproduce" is a founding idea of the Artificial Life field, ~35 years old. |

---

## What is *not* obviously shipped as one product

Being fair to the vision — a single unified product that combines **all** of:

1. LLM-driven inner lives (appraisal → buckets), **and**
2. a full embodied sensory model (vicinity, organ health from history), **and**
3. genetic **reproduction and open-ended evolution** across generations, **and**
4. agents **cloned from real, specific people**, **and**
5. packaged for **both games and "simulate humanity" research**

— I do not know of, as one shipping product. The *pieces* all exist and mostly
predate this; the *specific fusion* is plausibly greenfield. **But note what that
means:** the opening is an **execution and integration** play, not a novel idea and
not a patent. That is a normal, winnable kind of opportunity — it is just a different
one than "nobody has thought of this."

---

## The honest money read

- **You do not need a patent to make money here.** Almost none of the companies above
  have a core patent on "agents with feelings." They make money on **product,
  distribution, and execution** — the game is fun, the engine is easy to integrate,
  the clone is convincing. Inventing the concept earns nothing; shipping something
  people pay for earns money.
- **First-mover is already gone; that is survivable.** Most winners in a category are
  not first (Google wasn't the first search engine; Facebook wasn't the first social
  network). Being *better or different at a specific thing people pay for* beats being
  first at a broad idea.
- **The cost wall is real and is your hardest problem, not the idea.** Running
  thousands of LLM agents that appraise every event with a top model is *expensive* —
  this is the exact discipline the platform already fights (free deterministic floor,
  spend only on genuine decisions, hard budgets). Whoever makes a rich agent world
  **cheap** has the real moat. That is a technical edge worth more than a patent, and
  it is where your engineering instinct already points.
- **Pick one wedge, not the whole world.** "A model of our world as close as possible"
  is a research programme, not a first product. A single, sharp, sellable slice —
  e.g. *drop-in NPCs with real emotional memory for indie game studios*, or *a
  reproduce-and-evolve society sandbox as a paid game*, or *cost-efficient agent
  worlds as an API* — is where money actually starts.

---

## Where a defensible edge could genuinely be

Not the concept. Candidates that are *technical* and could be a real moat and/or the
narrow patent from PATENTABILITY.md §4:

- **Radical cost-efficiency** for large agent worlds (the free-floor / budgeted-spend
  discipline, taken further than anyone).
- A **stable long-horizon self model** — agents that change over thousands of events
  without dissolving or ossifying (nobody has cleanly solved this; RimWorld fakes it,
  Generative Agents drift).
- **Faithful cloning of a specific real person** with consent/likeness rights handled
  — the legal/ethical plumbing is itself a barrier others trip on.
- **Open-ended evolution of LLM-brained agents** (marrying ALife's reproduction with
  learned behaviour) — genuinely under-explored versus the two fields separately.

---

## Serious non-technical caveats before you spend money

- **Cloning real people** raises **likeness, publicity, privacy, and consent** law —
  in some places (e.g. parts of the US, EU) using someone's identity without consent
  is actionable regardless of your tech. This is a real cost centre.
- **"Reproduction" and "modelling humanity"** attract ethics and platform-policy
  scrutiny; investors and app stores will ask.
- The category is **capital-heavy** (compute) and **crowded**; validate demand with a
  narrow paid wedge before building the world.

---

### Bottom line

- Portraying humans/objects as code: **not patentable** — it is the definition of
  software.
- A product that does this: **yes, several already** — you are entering a hot,
  contested category, not an empty one.
- Can you still make money? **Yes — but on execution, cost, and a sharp wedge, not on
  the idea or a patent.** The best defensible edge you have is the one you already
  practise: making a rich agent world *cheap*.

*Market/company details as of early 2026; verify current status before relying on
them. Not legal or investment advice.*
