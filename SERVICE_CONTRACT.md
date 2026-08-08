# The service contract

A **module means a microservice.** A service is a directory you never have to read:
one process behind one port with one committed contract, green under
process-compose on first boot. These nine rules are the whole deal — the
`templates/service/` scaffold satisfies all of them out of the box, and
`services.yaml` is where a service becomes real.

1. **One process, one port, one database it alone opens.**
   The port comes from `PORT` (written into `data/env/<name>.env` by
   `tools/gen_fleet.py` from the service's `services.yaml` entry). The database is
   `data/<name>.db` (from `DB_PATH`), opened WAL, and **no other process ever opens
   that file** — isolation is structural: no handle, no join. Anyone who wants the
   data asks over HTTP.

2. **`GET /health` and `GET /openapi.json`, always.**
   `/health` returns readiness JSON `{ok, service, db, checks}` — `ok: true` only
   when the service could actually answer (its db opens and reads). process-compose
   probes it; dependents start on `process_healthy`. `/openapi.json` serves the
   **committed** `openapi.json` from the service directory — the contract artifact,
   never a live regeneration. Changed your routes? Regenerate
   (`python app.py --spec > openapi.json`), commit, and oasdiff gates the diff.

3. **`X-Service-Token`, checked in constant time.**
   Every endpoint beyond `/health`, `/openapi.json` and `/ui/*` requires the
   service's token (minted once by `tools/gen_fleet.py` into
   `data/tokens/<name>.token`, delivered as `SERVICE_TOKEN` env). Compare with
   `hmac.compare_digest` — a plain `!=` leaks the token one character at a time.
   No token configured = refuse everything: fail closed.

4. **Env-only config.**
   `PORT`, `DB_PATH`, `SERVICE_TOKEN`, peer `<NAME>_URL` entries and
   `CONDUCTOR_URL` arrive in the environment from `data/env/<name>.env`. No config
   files of your own, no dotenv loader, nothing read from another service's
   directory. Model credentials **never** appear here — inference goes through the
   conductor's model door.

5. **Events go through the conductor's door.**
   A service that wants something on the platform bus POSTs it to the conductor
   (`/internal/bus`, service-token authed, lands in P2) — it never writes the
   events table itself. The bus stays single-writer.

6. **Tests run offline.**
   `tests/` beside the code: a smoke test (in-process ASGI, no sockets) and a
   Schemathesis contract test driving the app against the **committed** spec. No
   network, no other services, no shared fixtures — the suite passes on a machine
   where nothing else is running.

7. **Every client documents degraded mode.**
   For each caller of this service, somewhere greppable, the answer to "and when
   it's down?" — e.g. "recall returns `[]` and never blocks a sprint". A client
   without a degraded-mode note is a client that will block a sprint at 3am.

8. **Acceptance = green under process-compose on first run.**
   Add the `services.yaml` block, run `python tools/gen_fleet.py`, then
   `./run-local.sh`: the service must come up healthy on the first boot, with its
   readiness probe passing and its own tests green. A service that needs manual
   fiddling to boot has not shipped.

9. **Nothing outside the directory imports inside it.**
   And nothing inside it imports the conductor or a sibling service. The ~60-line
   `helpers.py` (token check, WAL sqlite, tiny kv) is **vendored per service** —
   copied, not shared — precisely so this rule has no tempting exception. The
   directory's public surface is its HTTP contract, full stop.

## Optional: the `ui/` vertical slice

A service may ship its own face. If a `ui/` directory sits beside `app.py`, the
service serves it statically at `GET /ui/*` (no build step — same rules as the
dashboard), and the conductor's gateway proxies the whole service **same-origin**
at `/svc/<name>/…` — API and UI both, with the caller's session checked
conductor-side and the service token added server-side. Conventions:

- `ui/panel.html` — the embeddable card panel. Declare `ui: true` in the service's
  `services.yaml` entry and the Atlas embeds `/svc/<name>/ui/panel.html` (P6).
- `ui_screen: true` — the service additionally claims a full routed page in the
  shell.
- All fetches in `ui/` are **relative** (`../health`, not `/health`), so the same
  files work served directly and through `/svc/<name>/`.
- Headless services (knowledge, usage) simply delete `ui/` and stay API-only —
  honestly, with `ui: false`.

Net effect: dropping a service directory plus one `services.yaml` entry makes both
its API and its UI appear. Modules are pluggable vertical slices.
