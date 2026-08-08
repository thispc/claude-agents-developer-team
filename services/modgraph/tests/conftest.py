"""Test env for the modgraph service, set BEFORE app/helpers latch their config.

Offline by construction: a temp database, a known token, no legacy db and no
sockets — the suite drives the ASGI app in-process.

`REPO_ROOT` points at the real checkout, because the seed's whole job is to
describe the tree that is actually there — a seed drilled against a fixture tree
would prove only that it can read a fixture.

Three departures from the template's conftest, all because this suite must also
run INSIDE the full repo run (pytest.ini's testpaths include services/):

  1. The service is loaded under UNIQUE module names via importlib, because the
     conductor's `app` package is already in sys.modules when tests/ collected
     first — a plain `from app import app` would hit the wrong one.

  2. Every env var this file touches is SAVED and RESTORED after the load,
     because helpers/app read the environment at import time and the conductor
     suite's conftest pinned its own values.

  3. AND SO ARE THE MODULE NAMES. A service is launched as `uvicorn app:app
     --app-dir services/modgraph`, so its own files are TOP-LEVEL modules —
     `store`, `derive`, `seed`, `helpers`. `services/lifeworld` has a `store.py`
     too, and in a whole-repo run whichever mounted first would answer the
     other's `import store`. So the load owns those names for its duration and
     gives them back afterwards; the loaded modules keep direct references to
     each other, which is why nothing in this service imports inside a function.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parent.parent
REPO = SERVICE_DIR.parent.parent
_TMP = tempfile.mkdtemp(prefix="modgraph-svc-test-")

TOKEN = "test-service-token"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_ENV = {k: os.environ.get(k) for k in
        ("DB_PATH", "SERVICE_TOKEN", "SERVICE_NAME", "LEGACY_DB_PATH", "REPO_ROOT")}
os.environ["DB_PATH"] = str(Path(_TMP) / "modgraph-test.db")
os.environ["SERVICE_TOKEN"] = TOKEN
os.environ["SERVICE_NAME"] = "modgraph"
os.environ["LEGACY_DB_PATH"] = str(Path(_TMP) / "absent-legacy.db")   # no first-boot copy
os.environ["REPO_ROOT"] = str(REPO)
_OWN = sorted(p.stem for p in SERVICE_DIR.glob("*.py"))
_SAVED_MODS = {m: sys.modules.pop(m, None) for m in _OWN}
# app.py puts its own directory first on sys.path (so `uvicorn app:app` works from
# any cwd); left there it would shadow the conductor's `app` package for whatever
# runs next in a whole-repo pytest run.
_SAVED_PATH = list(sys.path)
try:
    helpers = _load("modgraph_service_helpers", SERVICE_DIR / "helpers.py")
    # app.py's own `import helpers` must find THIS configured instance, not a
    # fresh one latched to someone else's environment.
    sys.modules["helpers"] = helpers
    svc = _load("modgraph_service_app", SERVICE_DIR / "app.py")
    store = svc.store
    seed = svc.seed
    derive = svc.derive
finally:
    sys.path[:] = _SAVED_PATH
    for _m in _OWN:
        sys.modules.pop(_m, None)
    for _m, _mod in _SAVED_MODS.items():
        if _mod is not None:
            sys.modules[_m] = _mod
    for _k, _v in _ENV.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v


@pytest.fixture()
def clean_store():
    """The six tables and the layouts emptied for one test — cheaper than a fresh
    file, and the schema/backfill code ran once at load, exactly like a real boot."""
    con = svc.helpers.db()
    for table in svc.store.TABLES:
        con.execute(f"DELETE FROM {table}")
    con.execute("DELETE FROM kv WHERE key LIKE 'graph:pos:%'")
    con.commit()
    yield svc
