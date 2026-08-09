# devteam v1 — archived 2026-08-09

Frozen, not deleted. Tagged `v1-archive`. Everything below still runs.

## Running it

```sh
cd archive/v1
./run-local.sh            # the fleet: process-compose + 6 services
./run-local.sh --legacy   # conductor outside process-compose, services as children
```

Then <http://127.0.0.1:8787>, login `root` / `devteam`.

The virtualenv came along and its absolute paths were repointed at this new
location, so `.venv/bin/python` works from here as-is. Data is `devteam.db` and
`data/*.db` in this directory, exactly where it was.

## Why it was retired

It became unimprovable. 445 files, ~60K LOC, and two fatal ambiguities:

- **`module` meant two different things.** A microservice in one file, a work item
  in another. Nobody — human or agent — could hold both meanings at once.
- **The graph was authored, then reconciled.** Which meant it could assert an arrow
  the code did not have. A picture of the system that could lie about the system is
  worse than no picture.

## What v2 keeps from it

Ideas, not code. v2 shares no history and migrates nothing:

- the strangler discipline (extract with a dual-mode shim, then cut over deleting
  the fallback) — two commits, never one;
- the "one spend choke-point" and "un-leakable secret" invariants;
- the fleet's per-service WAL SQLite and committed-OpenAPI contract testing;
- the Atlas's rooms-and-doors navigation, and the hard lesson that **every node must
  carry `{file,line}` evidence or it must not render**;
- the repair crew's review gate and consult mechanism.

## Two known defects, never fixed, recorded here so they die with the archive

1. `selfops.head()` forks `git` five times per Atlas poll — about 64ms of the
   remaining 93ms Atlas latency after connection pooling landed (`d6e7ecb`).
2. `deliverables/` has no garbage-collection bound. It grows without limit.

## One thing that is NOT archived

`.env` in this directory holds live credentials. The exposed GitHub PAT and Claude
OAuth token flagged during the v1 work **still need rotating.** Archiving the code
did not rotate them.
