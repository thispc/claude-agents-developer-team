# run-tests

**This file is not hashed.** Reword it, restructure it, rewrite it in another
language — `contract_id` does not move and nothing rebuilds. It exists to tell an
agent what to aim at, which makes it a search heuristic rather than an identity.
The thing that *is* identity lives in `interface.json`, `tests/` and
`toolchain.json`.

## What it does

Runs a test command in a directory and reports what failed as structure.

The kernel does not need the structure. For admission the exit code is the whole
signal, on purpose — anything that parses output is somewhere a judgement call
could creep back into the trusted base. This module exists for the agent that has
to *fix* a failure, which cannot act on "exit 1".

## The rules it must always obey

- **Exit 0 is a pass and nothing else is.** Output that says `ok 1 - everything
  is fine` alongside a non-zero exit is a failure.
- **A hang is killed at the deadline**, and killed as a process group, because a
  runner that spawned workers of its own would otherwise hold the sandbox open
  past its timeout.
- **Output that is not TAP is reported, not treated as an error.** Plenty of
  runners are not TAP. Turning "your tests are in a different format" into "your
  tests failed" would be a lie that costs an agent an entire debugging round.
- **The summary is one line, always.** Every view that shows it is
  line-oriented; a newline in there breaks all of them.
- **Output is capped.** A test that loops printing must not be able to exhaust
  the memory of the thing observing it.

## Shared state

None. This module holds nothing between calls, reads no configuration, and knows
about no database. Everything it needs arrives in the input — which is why it can
be re-derived from scratch by anyone without coordinating with anything else.

*(Naming shared-state dependencies here is required when there are any. The
measured dominant failure of agent-written systems is "feature isolation" —
components that are each correct and fail to share state — and it is prevented
when contracts are authored, not when tests are run.)*
