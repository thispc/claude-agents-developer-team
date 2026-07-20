# Plan mode: the round table

Before hiring engineers, a circle of diverse agents deliberates and produces a
blueprint. This document records **why the arrangement is what it is**, and —
more importantly — **what the evidence does and does not support.** Read the
next section before deciding a table is worth its cost.

---

## Read this first: what the evidence does NOT support

**Multi-agent debate does not reliably beat one good model talking to itself —
and heterogeneity does not clearly fix that.** Be honest about this before
spending 3N+1 model calls on a table.

[Estornell & Liu (2025)](https://arxiv.org/abs/2502.08788) found debate
frameworks "fail to reliably outperform simple single-agent baselines such as
Chain-of-Thought and Self-Consistency, even when consuming additional
inference-time computation," and sometimes flip *correct* answers to wrong ones.

Model heterogeneity is the one intervention that consistently helps — but it
helps *MAD*, which is not the same as beating a single agent.

**The paper never compares Heter-MAD to Self-Consistency.** Its only baseline is
"CoT-Average" — the *mean* of the two pooled models' scores, which includes the
weaker one. That is how "+29.3% on MATH" is produced: SoM-Heter scores 71.1 on
MATH, while GPT-4o-mini's plain CoT alone scores **72.87**. The headline gain is
an artefact of averaging in the weaker model.

Doing the comparison the paper omits, using its own Tables 3/4/7 (SoM-Heter vs
Self-Consistency, GPT-4o-mini + Llama3.1-70b pool):

| Benchmark | SoM-Heter | Best SC | |
|---|---|---|---|
| MMLU | 83.5 | 83.73 | lose |
| MMLU-Pro | 65.0 | 66.27 | lose |
| CommonsenseQA | 83.3 | 83.80 | lose |
| ARC-Challenge | 92.1 | 93.93 | lose |
| AGIEval | 70.1 | 67.07 | **win** |
| GSM8K | 94.6 | 95.67 | lose |
| MATH | 71.1 | 73.96 | lose |

**Heterogeneous debate beats Self-Consistency on 1 of 7 benchmarks.** The
paper's own conclusion, written *after* the Heter-MAD results, is not retracted:
"existing MAD frameworks fail to reliably outperform simple single-agent
baselines like CoT and SC, despite consuming additional computational resources."

One real caveat, buried in Appendix D.1: heterogeneity *stacked with* CoT
prompting (Heter-SoM-CoT) does beat SC on 6 of 7. So heterogeneity plus a strong
single-agent technique can edge ahead; heterogeneity alone cannot.

On compute: "in comparison to SC, MAD is generally a less efficient method for
leveraging token consumption." No MAD method achieved a >20% win rate against
plain CoT across 36 configurations.

### So why does this feature exist?

Because **every one of those benchmarks is a closed-form question with one
correct answer.** GSM8k, MMLU, HumanEval. Self-Consistency works there precisely
because you can majority-vote your way to the right number. *You cannot
majority-vote a system design.*

For open-ended work the evidence **splits**, and the split is the single most
important thing on this page:

- **Divergent generation (ideation): supported.** [Multi-agent AI systems
  outperform human teams in creativity](https://arxiv.org/abs/2605.17885) scored
  4,541 ideas across six open-ended problems, blind-rated by five human judges,
  and found multi-agent beats a *single-agent baseline* with d=0.52 (GPT-4.1) and
  d=0.61 (o3-low) — driven by novelty. Note the effect shrinks as the base model
  gets stronger, the same pattern that kills MAD on QA.

- **Convergent deliberation: actively contradicted.** [The Deliberative
  Illusion](https://arxiv.org/abs/2606.03032) studied exactly the no-right-answer
  regime and found multi-agent discussion **erases up to 72% of issue-critical
  facts**, with positions collapsing toward base-model defaults rather than toward
  anything the discussion produced. Agents end up *more aligned and less
  informed*.

Planning is a convergent task — it must end in one plan. So the failure mode
documented in that second paper is the one this feature is most exposed to, and
no study was found showing multi-agent debate beating a comparable-compute single
agent on a design or planning task specifically.

The reading that fits both results: **the gain comes from the fan-out, not from
the debate.** Sampling several genuinely different minds is what adds something;
arguing them toward consensus is what destroys information.

That means the justification for a round table is **not** "it gives more correct
answers." The claim it can actually support is narrower:

> A structured argument between different models produces a better *artifact*
> than a single pass does — one that records the alternatives it rejected, the
> risks it found, and the objection it could not answer.

That is a **design bet, not a proven result** — and the strongest counter-evidence
(fact attrition and stance homogenisation during deliberation) is aimed squarely
at it. Three things in the implementation exist specifically to blunt it:

1. **Round 1 is preserved and re-read at synthesis.** The moderator is given the
   original independent proposals *and* told to look for facts that appeared in
   round 1 and then vanished — the exact attrition the Deliberative Illusion
   measured.
2. **Dissent is an output field, not something consensus is allowed to dissolve.**
3. **Diverge-only mode** skips the debate rounds entirely: independent proposals
   straight to synthesis. It is `N+1` calls instead of `3N+1`, and it is the
   configuration the evidence actually supports. Use it when you want options;
   use the full table when you want the argument stress-tested and accept the risk.

If you only want an answer to a question with one right answer, ask one good
model — that is cheaper and the evidence says it is at least as good.

Given that: a table of five identical models is the *worst* configuration —
strictly more expensive than one call with no measured upside. If you run a
table, vary the seats. And [Woolley et al. (Science, 2010)](https://www.science.org/doi/10.1126/science.1193147)
found collective intelligence is "not strongly correlated with the average or
maximum individual intelligence of group members," so putting your best model in
every chair is not the win it feels like either.

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
| Heterogeneity is the only reliably helpful lever | Per-seat provider + model + key. Warn when all seats share one provider, and harder when they share one model. |
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
