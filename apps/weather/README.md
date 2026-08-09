# weather — the demo that proves the model

A working app, built the way every devteam project will be built. Five modules,
one human-written shell, and two of the five are Python without the other three
knowing.

```sh
node bin/devteam.js --in=apps/weather build      # verify and admit all five
node apps/weather/serve.js "London, UK" --open    # run it, live
node apps/weather/serve.js --offline --json       # run it against the fixture
```

---

## How it works, end to end

```
  "London, UK"
       │
       ▼
  parse-place ──────────► { name: "London", region: "UK" }
       │
       │  ┌───────────────────────────────────────────────────────┐
       └─►│  THE SHELL asks the provider for candidates           │  ← the only
          │  (serve.js — the edge tier, and NOT a module)         │    impure part
          └───────────────────────────────────────────────────────┘
       │
       ▼
  match-place ──────────► which candidate "UK" meant
       │
       │  ┌───────────────────────────────────────────────────────┐
       └─►│  THE SHELL asks for that place's forecast             │
          └───────────────────────────────────────────────────────┘
       │
       ▼
  normalise-forecast ───► the canonical shape: { place, days[] }
       │                              │
       ▼                              ▼
  advise-clothing ─────► advice ─► render-forecast ─► one HTML page
```

## The one rule that shapes everything: I/O lives at the edge

`verify()` runs every module in a container with **`--network=none`**. A module
that fetched a forecast could therefore never be verified — and a gate you switch
off for one module is not a gate.

So the two network calls live in `serve.js`, in about thirty lines a person owns
and reads. Everything else is a pure function of what it is handed. That is not a
workaround for the sandbox; it is the arrangement that makes the other five
modules **swappable, cacheable, and checkable for determinism** — none of which is
true of anything that calls out to a network.

`serve.js` has no contract and no conformance suite, deliberately, because it is
exactly the part that cannot have one. Keeping it small is the only control
available. **When it grows, logic moves down into a module rather than the shell
growing tests** — and that happened during this build: choosing between search
candidates started as three lines in the shell, they were wrong (`London, UK`
found nothing, because the provider says `United Kingdom`/`GB`), and the fix was
`match-place`, not a bigger shell.

## What the swap actually proved

`normalise-forecast` and `advise-clothing` were written in JavaScript, admitted,
then **deleted and rewritten in Python**. The contract ids did not move:

```
c-0e99668b…  normalise-forecast
     a-8dbd639cedef  js   68 lines   proved: size, conformance, determinism, heldout
   → a-a9eb6a25b74d  py   86 lines   proved: size, conformance, determinism, heldout

c-94c01259…  advise-clothing
     a-7862ce083508  js   46 lines   proved: size, conformance, determinism, heldout
   → a-56ef41a3275e  py   61 lines   proved: size, conformance, determinism, heldout
```

Then the app ran again — three JavaScript modules and two Python ones in one
pipeline — and produced **byte-identical output**. `serve.js` does not know which
languages it is talking to, because it spawns each module and speaks JSON down a
pipe rather than importing it.

`devteam pin` switches between them without touching a file, and the output stays
identical either way.

### Two things the swap caught

**The rounding rule.** `normalise-forecast` rounds temperatures, and the obvious
call in each language disagrees: Python's `round()` is banker's rounding, so
`round(2.5)` is **2**; JavaScript's `Math.round(-2.5)` is **-2**. Two honest
implementations would differ by one degree with nothing to explain why. The
contract states *half away from zero* and pins both boundaries, so the Python
version had to implement it deliberately.

**A real kernel bug.** Pinning the JavaScript artifact back while the working copy
said Python made the composer try to start a file that artifact did not contain —
because it read `language` and `entry` from the working copy instead of from the
artifact. The toolchain *is* an artifact property; that is the whole reason it
sits on that side of the hash. Fixed, with a regression test.

## The five modules

| module | what it does | why it is separate |
|---|---|---|
| `parse-place` | text → `{name, region}` | splits on the **first** comma only; never changes case, because case-folding is locale-sensitive |
| `match-place` | candidates + region → which one | holds the `UK`→`United Kingdom` courtesy list; tries fields most-specific-first |
| `normalise-forecast` | provider JSON → canonical | **exactly one module knows what open-meteo looks like**, so changing provider touches nothing else |
| `advise-clothing` | canonical → what to take | holds all three thresholds; changing one is a contract change, which is correct |
| `render-forecast` | canonical + advice → HTML | self-contained page; escapes everything a provider supplied |

Every one is **portable** — nothing in any `[contract]` names a language — and
every one passes a determinism gate that runs its suite twice under a different
hash seed, locale, timezone, hostname and umask.

## Where a project's files live

```
apps/weather/
  wiring.toml     the graph. A HUMAN writes this. Agents never do.
  serve.js        the shell. Impure, small, contract-free, and read by a person.
  modules/        five folders, each a contract plus one implementation
  heldout/        vaults — tests the implementing agent never sees
  fixtures/       one recorded provider response, so runs are repeatable
```

The artifact store is **not** in here. It lives once at the repo root, shared by
every workspace, because artifacts are content-addressed — two projects that
build the same bytes share them, and a module lifted from one project into
another is already admitted.
