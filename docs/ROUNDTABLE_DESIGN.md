# Plan mode: the round table

Before hiring engineers, a circle of diverse agents deliberates and produces a
blueprint. This document records **why the arrangement is what it is** — every
rule below comes from published research on group decision quality, human or
LLM. The seating chart is not decoration; it is the mechanism.

---

## The single most important finding

Multi-agent debate does **not** reliably beat one good model talking to itself.
[Estornell & Liu (2025)](https://arxiv.org/abs/2502.08788) evaluated the popular
debate frameworks and found they "fail to reliably outperform simple single-agent
baselines such as Chain-of-Thought and Self-Consistency, even when consuming
additional inference-time computation" — and sometimes flip *correct* answers to
wrong ones.

The intervention that did work, consistently, was **model heterogeneity**: their
Heter-MAD lets each agent draw from a pool of different foundation models, and
that "consistently improved performance across the various benchmarks evaluated."

**So: a round table of five Claude seats is theatre. A round table of Claude +
GPT + Gemini is the actual mechanism.** Cross-provider is not a nice-to-have
feature of this design — it is the reason the design works at all.

This also matches [Woolley et al. (Science, 2010)](https://www.science.org/doi/10.1126/science.1193147),
who found a group's collective intelligence is "not strongly correlated with the
average or maximum individual intelligence of group members." Putting your best
model in every chair is not the win it feels like.

---

## The arrangement, and the evidence for each rule

### 1. Round one is silent and independent

Each seat writes its own proposal **without seeing any other seat's**.

*Why:* interactive groups produce fewer and less original ideas than the same
people working alone, because of **production blocking** — you cannot generate
while listening. The Nominal Group Technique fixes this by enforcing independent
written generation before discussion, and outperforms open brainstorming on both
quantity and quality of ideas.

*Also:* it removes the anchor. LLM debates show "a robust first-mover advantage
… with the initiating agent consistently winning far above chance"
([2607.05545](https://arxiv.org/pdf/2607.05545)). If nobody speaks first, nobody
anchors.

### 2. Round two is structured dissent, not discussion

Every seat must attack the others' proposals — name the weakest assumption, the
thing that breaks at scale, the requirement being ignored.

*Why:* comparing dialectical inquiry, devil's advocacy and consensus,
[Schweiger, Sandberg & Ragan (AMJ)](https://journals.aom.org/doi/10.5465/255859)
found both structured-conflict methods "led to higher quality recommendations and
assumptions than consensus," with dialectical inquiry best at surfacing hidden
assumptions. Consensus groups were *happier* and more committed — and produced
worse decisions. Agents do not need to be happy.

One seat additionally holds a standing **skeptic** brief, because assigned
opposition beats hoping someone objects.

### 3. Turn order rotates; every seat speaks once per round

*Why:* Woolley found collective intelligence is predicted by "the equality in
distribution of conversational turn-taking" — "groups where a few people
dominated the conversation had less collective intelligence." Equal airtime is
enforced mechanically here, which is easier than it is for humans.

### 4. A moderator in the middle synthesises — it does not vote

The centre seat reads everything and writes the blueprint, weighing *arguments*,
not counting agreements.

*Why:* sycophancy and peer conformity make agents converge prematurely —
"fostering premature consensus and stifling critical discourse"
([2509.23055](https://arxiv.org/html/2509.23055v1)). A majority of agreeing
agents is evidence of conformity as often as of truth, so the synthesiser is
explicitly told that unanimity is a warning sign, and that a lone well-argued
dissent may outweigh three agreements.

### 5. Three to six seats

*Why:* groups of 3–6 are "much more productive and developmentally advanced"
than 7+; deliberative conversation degrades past 5–7 as turn-taking collapses;
one line of work puts the optimum near 4.6. Past seven, each added member is
estimated to cost ~10% of decision effectiveness.

Default **4**. Minimum 3. Above 6 the UI warns you.

---

## What this produces

A **blueprint**: the problem restated, the chosen approach with its rationale,
the alternatives considered and why they lost, the risks with mitigations, and a
proposed team of roles. The dissent is preserved in the artifact — a plan that
records what its strongest critic said is more useful than one that hides it.

The blueprint then feeds the existing recruiting flow: roles become the roster,
and the manager builds the DAG from the chosen approach.

---

## Design consequences for the implementation

| Rule | Implementation |
|---|---|
| Heterogeneity is the mechanism | Per-seat provider + model + key. Warn when all seats share one provider. |
| Independent round 1 | Seat prompts for round 1 contain no other seat's text. |
| Structured dissent | Round 2 prompt *requires* naming a specific flaw; one seat is a standing skeptic. |
| Equal turn-taking | Fixed one-turn-per-seat-per-round; rotate starting seat each round. |
| Anti-conformity | Synthesiser told unanimity is suspicious; dissent preserved in output. |
| 3–6 seats | Enforced min 3, warn above 6. |

---

## Sources

- [Improving Factuality and Reasoning in Language Models through Multiagent Debate](https://arxiv.org/abs/2305.14325) — Du, Li, Torralba, Tenenbaum, Mordatch (ICML 2024)
- [If Multi-Agent Debate is the Answer, What is the Question?](https://arxiv.org/abs/2502.08788) — the critique, and Heter-MAD
- [Evidence for a Collective Intelligence Factor in the Performance of Human Groups](https://www.science.org/doi/10.1126/science.1193147) — Woolley et al., Science 2010
- [Group Approaches for Improving Strategic Decision Making](https://journals.aom.org/doi/10.5465/255859) — dialectical inquiry vs devil's advocacy vs consensus
- [Peacemaker or Troublemaker: How Sycophancy Shapes Multi-Agent Debate](https://arxiv.org/html/2509.23055v1)
- [Most LLM Conformity Needs No Speaker](https://arxiv.org/pdf/2607.05545) — first-mover advantage
