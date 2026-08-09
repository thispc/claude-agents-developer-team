# render-forecast

**Not hashed.** Reword freely.

Turns a canonical forecast and its advice into one self-contained page.

- **No external stylesheet, script or font.** The page has to work with the
  network off, which is the same condition its own tests run under.
- **Place names are escaped.** A name arrives from a search provider, which makes
  it untrusted text landing in a document.
- **Numbers are formatted by hand, never with `toLocaleString`** — that reads
  `LANG` at runtime, and the determinism gate would refuse it.

## Shared state
None.
