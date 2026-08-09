# The modularization refactor — target and execution record

Owner's ask: "make the code more modular and reusable, check for redundancies, and have code
such that I can explain it in a simple architecture diagram."

## Target architecture (what ARCHITECTURE.md must show when done)

```
BACKEND (conductor/app)
┌─ api layer ────────────────────────────────────────────────┐
│ routes/ (package: one module per domain, one shared base)  │
│ lifeworld_routes · repair_routes · logs_routes             │
│           all guards from app/guards.py                    │
├─ domains ──────────────────────────────────────────────────┤
│ projects: manager · launcher · scheduler · team · review…  │
│ repair:   repair · repair_builder · monitor · selfops      │
│ lifeworld: world (facade) · scene · human · knowledge…     │
│           upward needs go through lifeworld/ports.py ONLY  │
├─ kernel ───────────────────────────────────────────────────┤
│ db · config · bus · auth · tuning · usage · logs ·         │
│ providers · shell (the ONE subprocess/git wrapper)         │
└────────────────────────────────────────────────────────────┘

FRONTEND (dashboard/js, classic scripts, order load-bearing)
core (shell: auth, router, api, ws var)
→ lib (utilities + design system: escapeHtml/trim/ago/toast/
       waitForRestart/ui* builders/markdown/sigil/avatars)
→ projects (the work view + HQ seam) → ops (deploy screens)
→ studio-legacy (retired scenes engine, Teams entry)
→ studio → canvas1 (v1 engine only) → agent → repair → boot
canvas2/ (ES modules) talks to the rest via window.* only
```

## Work packages (disjoint file sets, run in parallel)

- **C — routes package**: split routes.py (2668 lines, 137 endpoints, contiguous domain
  ranges) into `app/routes/` package; extract `app/guards.py`; update the endpoint-count
  gate + HANDBOOK. Keeps `app.routes.manager` etc. importable (monkeypatch paths) and
  `from app.routes import Settings`.
- **D — kernel + ports**: `app/shell.py` unifying the 6 subprocess wrappers;
  `lifeworld/ports.py` consolidating the substrate's 11 upward imports; `_wait_healthy`
  dedupe (deploy/sandbox).
- **E — frontend lib**: new `js/lib.js` (after core, before projects) receiving the
  platform-wide helpers stranded in projects.js/studio-legacy.js/canvas1.js head; new
  `js/ops.js` for the deploy/ops screens stranded in core.js; index.html order + pin
  updates in tests.

Accepted debt (documented, not fixed now): repair↔monitor mutual lazy imports; the five
time-ago variants (different semantics); studio-legacy's 53 internal functions (retired
subsystem, still the Teams entry); routes↔main STARTED_AT lazy import.

Rules for every package: suite-green before done; run only targeted tests during work
(the full suite runs once at the end, dev server stopped); no behavior changes — moves,
renames and dedup only; APPEND ONLY migrations untouched; handbook gates updated in the
same commit as the change they gate.
