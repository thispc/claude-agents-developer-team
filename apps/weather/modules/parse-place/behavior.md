# parse-place

**Not hashed.** Reword freely.

Turns what a person typed into a place query.

Splits on the **first** comma only: `Paris, Texas, USA` is a place called Paris in
a region called `Texas, USA`. Splitting on every comma would invent a third field
nobody asked for.

**It does not change case**, and that is deliberate rather than lazy. Case-folding
is locale-sensitive — in Turkish, `i` upper-cases to `İ` — so a module that
title-cased would answer differently under different locales. The determinism
gate would refuse it, correctly. The provider's search is case-insensitive anyway.

## Shared state
None.
