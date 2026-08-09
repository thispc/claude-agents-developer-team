# match-place

**Not hashed.** Reword freely.

Given the places a search returned and the region a person typed, says which one
they meant.

## Why it is a module and not three lines in the app shell

It was three lines in the shell, and they were wrong: the provider answers
`United Kingdom` / `GB`, so a person typing `London, UK` got "no such place".
Fixing it in the shell would have put a growing alias table in the one file that
has no contract and no tests.

So it moved down here, which is the rule the shell states about itself: when the
edge grows, push logic into a module rather than write tests for the edge.

## The rules

- **No region means no opinion** — the provider already ranked its results.
- **Fields are tried most-specific-first**, and every candidate is checked at each
  level before moving on. Otherwise a country match on the first result would
  beat a county match on the second, which is backwards.
- **The alias list is short on purpose.** It is a courtesy for `UK`, `USA` and a
  few others, not an attempt at a world atlas. Every entry is a judgement someone
  could disagree with — which is exactly why it belongs in one module with a
  conformance suite rather than scattered across a shell.

## Shared state
None.
