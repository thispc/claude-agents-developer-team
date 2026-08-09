# render-graph

**Not hashed.** Reword freely; nothing rebuilds.

## What it does

Turns `wiring.toml` and the ledger into the payload the Atlas draws.

## The one rule that matters

**A node or edge that cannot name a real line in a real file is not drawn.**

This is the failure v1 died of, so it is worth stating plainly. v1's graph was
authored by a planner and then reconciled against the code, which meant it could
assert an arrow the code did not have. A picture of the system that is capable of
lying about the system is worse than no picture, because you stop checking.

So evidence is not a field that gets filled in when convenient. It is
`minItems: 1` in the output schema, and this module finds each line by reading
the raw wiring text rather than trusting the parsed structure — the claim being
made is "open this file at this line and you will see it", and the only way to
keep that claim honest is to have looked.

Anything dropped goes into `dropped[]` with a reason. Silence would be the same
failure wearing a quieter face: a graph that renders nine of ten nodes and says
nothing reads exactly like a graph of nine nodes.

## Status comes from the ledger, never from an opinion

- **live** — an artifact is admitted (or pinned) for this module's contract.
- **refused** — attempts exist and every one was refused.
- **unbuilt** — nothing has been tried yet. Not the same as broken, and not
  drawn as though it were.

A refusal that happened *after* an admission leaves the node **live**, and the
note says so out loud: something was tried, it did not pass, and what was already
running carried on. That is the system working exactly as designed, and a viewer
that showed it as a problem would be teaching the operator to distrust a healthy
signal.

A **pin** outranks any later admission, because a pin is a human saying "no,
this one" and auto-admit must not be able to overrule it.

## Shared state

None. Everything arrives in the input.
