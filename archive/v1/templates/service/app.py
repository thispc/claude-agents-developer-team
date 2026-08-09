"""A devteam fleet service — the golden-path scaffold.

Instantiate by copying this directory (`cp -r templates/service services/<name>`),
adding a block to services.yaml, and re-running tools/gen_fleet.py. The service's
name derives from its directory (override with SERVICE_NAME), so a fresh copy
boots with zero edits. The rules this file already satisfies are the repo-root
SERVICE_CONTRACT.md; what to fill in is SERVICE.md beside this file.

Endpoints the contract requires:
  GET /health        readiness JSON {ok, service, db, checks} — process-compose probes it
  GET /openapi.json  the COMMITTED contract (openapi.json here), never a live regeneration
Plus a demonstration kv pair (GET/PUT /kv/{key}) proving the db and the
X-Service-Token check work — replace it with your real API and re-run
`python app.py --spec > openapi.json` to update the contract.
"""

import json
import os
import sys
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))       # so `uvicorn app:app` works from any cwd
import helpers                      # vendored per service — never imported across services

SERVICE = os.environ.get("SERVICE_NAME", HERE.name)
SPEC = HERE / "openapi.json"

# openapi_url=None: FastAPI must not serve a live-generated spec at the contract's
# address — the committed file is the contract, and drift between the two is a
# thing the contract tests exist to catch.
app = FastAPI(title=SERVICE, openapi_url=None)


@app.get("/health")
def health() -> dict:
    """Readiness, not liveness: ok only when the service could actually answer —
    which for anything stateful means its own database opens and reads."""
    checks = {"db": helpers.db_ok()}
    return {"ok": all(checks.values()), "service": SERVICE,
            "db": str(helpers.DB_PATH), "checks": checks}


@app.get("/openapi.json")
def openapi_spec() -> JSONResponse:
    """The committed contract. Regenerate after changing routes:
    `python app.py --spec > openapi.json` — and let oasdiff judge the diff."""
    return JSONResponse(json.loads(SPEC.read_text()))


class KvValue(BaseModel):
    value: str


@app.get("/kv/{key}", dependencies=[Depends(helpers.require_token)])
def kv_get(key: str) -> dict:
    return {"key": key, "value": helpers.kv_get(key)}


@app.put("/kv/{key}", dependencies=[Depends(helpers.require_token)])
def kv_put(key: str, body: KvValue) -> dict:
    helpers.kv_set(key, body.value)
    return {"key": key, "ok": True}


# The optional vertical slice: a ui/ dir beside app.py is served at /ui/* and the
# conductor proxies it same-origin at /svc/<name>/ui/ — delete the dir for a
# headless service and nothing else changes.
if (HERE / "ui").is_dir():
    app.mount("/ui", StaticFiles(directory=str(HERE / "ui"), html=True), name="ui")


if __name__ == "__main__":
    if "--spec" in sys.argv:
        # The one honest way to update the contract: print what the code actually
        # serves, commit it, and let the tests + oasdiff hold you to it.
        spec = app.openapi()
        spec["info"]["title"] = "service"       # instance-independent, so the template's
        print(json.dumps(spec, indent=2))       # committed spec matches every copy
    else:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8880")))
