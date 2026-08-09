# inspect-ui

**Not hashed.** Reword freely.

Renders the Atlas payload as a page you can open and operate.

## Why this is a module and not just a file

Because a dashboard written outside this system would be the one part of the
platform nobody could verify — and unaccountable code is what made v1
unimprovable. So it has a contract, a conformance suite, a size cap, and it is
admitted or refused like anything else. If it breaks, the gate says so before you
ever open a browser.

## The rules

- **A pure function of its input.** No filesystem, no clock, no network, no state
  between calls. The same payload gives the same bytes, which is the only reason
  a conformance case can pin it at all. The server that serves the page does the
  impure work.
- **Everything authored elsewhere is escaped.** Module names, notes and edge
  reasons are written by planners and agents, which makes every one of them
  untrusted text arriving in a document. A module called
  `<script>alert(1)</script>` renders as its own name.
- **Self-contained.** No external stylesheet, script or font. The page has to work
  on a laptop with the network off, which is the same condition its own tests run
  under.
- **Trouble first.** Cards are ordered refused, then not-built, then live. Sorting
  by name makes you hunt for the one thing that needs attention.
- **A node with no evidence is called out, never drawn as though it were
  complete.** Those should have been dropped upstream; if one arrives anyway,
  saying so is better than quietly rendering a plausible card.

## Shared state

None. Everything arrives in the input.
