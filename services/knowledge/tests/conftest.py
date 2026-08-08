"""Test env for the knowledge service, set BEFORE app/helpers latch their config.

Offline by construction: a temp database, a known token, no legacy db and no
sockets — the suite drives the ASGI app in-process.

Two departures from the template's conftest, both because this suite must also
run INSIDE the full repo run (pytest.ini's testpaths include services/):

  1. The service is loaded under UNIQUE module names ("knowledge_service_app",
     "knowledge_service_helpers") via importlib, because the conductor's `app`
     package is already in sys.modules when tests/ collected first — a plain
     `from app import app` would hit the wrong one. Test files fetch the loaded
     modules with a plain `import knowledge_service_app`, which resolves from
     sys.modules (this conftest always imports before its test files).

  2. Every env var this file touches is SAVED and RESTORED after the load,
     because helpers/app read the environment at import time and the conductor
     suite's conftest pinned its own values — leaking DB_PATH across suites is
     how one suite's fixture deletes another suite's database.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parent.parent
_TMP = tempfile.mkdtemp(prefix="knowledge-svc-test-")

TOKEN = "test-service-token"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_ENV = {k: os.environ.get(k)
        for k in ("DB_PATH", "SERVICE_TOKEN", "SERVICE_NAME", "LEGACY_DB_PATH")}
os.environ["DB_PATH"] = str(Path(_TMP) / "knowledge-test.db")
os.environ["SERVICE_TOKEN"] = TOKEN
os.environ["SERVICE_NAME"] = "knowledge"
os.environ["LEGACY_DB_PATH"] = str(Path(_TMP) / "absent-legacy.db")   # no first-boot copy
_SAVED_HELPERS = sys.modules.pop("helpers", None)
try:
    helpers = _load("knowledge_service_helpers", SERVICE_DIR / "helpers.py")
    # app.py's own `import helpers` must find THIS configured instance, not a
    # fresh one latched to someone else's environment.
    sys.modules["helpers"] = helpers
    svc = _load("knowledge_service_app", SERVICE_DIR / "app.py")
finally:
    sys.modules.pop("helpers", None)
    if _SAVED_HELPERS is not None:
        sys.modules["helpers"] = _SAVED_HELPERS
    for _k, _v in _ENV.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v


@pytest.fixture()
def clean_store():
    """A knowledge table emptied for one test — cheaper than a fresh file, and the
    schema/backfill code ran once at load, exactly like a real boot."""
    svc.helpers.db().execute("DELETE FROM knowledge")
    svc.helpers.db().commit()
    yield svc
