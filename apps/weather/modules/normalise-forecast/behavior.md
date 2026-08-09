# normalise-forecast

**Not hashed.** Reword freely.

Turns one provider's payload into the canonical shape every other module reads.

## Why it exists

So that **exactly one module knows what open-meteo's JSON looks like.** Changing
provider is then a new implementation of this contract, and touches nothing else.
Without it, the provider's field names would be smeared across four modules and
"switch provider" would be a rewrite.

## The rounding rule, spelled out

Temperatures round to whole degrees and rain to one decimal, **half away from
zero** — and it is stated rather than delegated because the obvious call in each
language disagrees. Python's `round()` is banker's rounding, so `round(2.5)` is
**2**. JavaScript's `Math.round` is half-up toward positive infinity, so
`Math.round(-2.5)` is **-2**. Two honest implementations would differ by one
degree and nothing would explain why. Both boundaries are pinned by cases.

**Ragged columns are refused.** Column-oriented APIs fail by returning one short
array; a naive reader then emits days with missing fields. Half a forecast must
not look like a whole one.

**Dates pass through untouched**, never re-derived from a clock.

## Shared state
None.
