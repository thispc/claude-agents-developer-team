# advise-clothing

**Not hashed.** Reword freely.

Given a canonical forecast, says what to take out of the house.

## The opinions, and where they live

Three thresholds, and they live here and nowhere else:

- below **5°** low → a warm coat
- above **25°** high → a sun hat
- **1mm** or more of rain → an umbrella
- any snowy day → boots · any stormy day → a reason to stay in

Changing one is a contract change, because the conformance suite pins each
boundary exactly. That is correct rather than annoying: "when is it cold enough
for a coat" is the entire opinion this module exists to hold, and an opinion that
can drift silently is not an opinion, it is a bug waiting.

**Items are sorted by codepoint, never by locale.** `localeCompare` reads `LANG`
at runtime, which would make the answer depend on the machine — the exact thing
the determinism gate refuses.

## Shared state
None.
