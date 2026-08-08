"""Test env for the usage service, set BEFORE app/helpers latch their config.

Offline by construction: a temp database, a known token, no legacy db, no
conductor and no sockets — the suite drives the ASGI app in-process and answers
the knob hop with a transport of its own.

Two departures from the template's conftest, both because this suite must also
run INSIDE the full repo run (pytest.ini's testpaths include services/):

  1. The service is loaded under UNIQUE module names ("usage_service_app",
     "usage_service_helpers") via importlib, because the conductor's `app`
     package is already in sys.modules when tests/ collected first — a plain
     `from app import app` would hit the wrong one. Test files fetch the loaded
     modules with a plain `import usage_service_app`, which resolves from
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

import httpx
import pytest

SERVICE_DIR = Path(__file__).resolve().parent.parent
_TMP = tempfile.mkdtemp(prefix="usage-svc-test-")

TOKEN = "test-service-token"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_ENV = {k: os.environ.get(k)
        for k in ("DB_PATH", "SERVICE_TOKEN", "SERVICE_NAME", "LEGACY_DB_PATH",
                  "CONDUCTOR_URL")}
os.environ["DB_PATH"] = str(Path(_TMP) / "usage-test.db")
os.environ["SERVICE_TOKEN"] = TOKEN
os.environ["SERVICE_NAME"] = "usage"
os.environ["LEGACY_DB_PATH"] = str(Path(_TMP) / "absent-legacy.db")   # no first-boot copy
os.environ["CONDUCTOR_URL"] = "http://conductor.test"
_SAVED_HELPERS = sys.modules.pop("helpers", None)
# app.py puts its own directory first on sys.path (so `uvicorn app:app` works from
# any cwd); left there it would shadow the conductor's `app` package for whatever
# runs next in a whole-repo pytest run.
_SAVED_PATH = list(sys.path)
try:
    helpers = _load("usage_service_helpers", SERVICE_DIR / "helpers.py")
    # app.py's own `import helpers` must find THIS configured instance, not a
    # fresh one latched to someone else's environment.
    sys.modules["helpers"] = helpers
    svc = _load("usage_service_app", SERVICE_DIR / "app.py")
finally:
    sys.path[:] = _SAVED_PATH
    sys.modules.pop("helpers", None)
    if _SAVED_HELPERS is not None:
        sys.modules["helpers"] = _SAVED_HELPERS
    for _k, _v in _ENV.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v


# --- the conductor's tuning door, as a transport ------------------------------
#
# The service reads the owner's dials over HTTP. Here that hop is answered by a
# stub with the same JSON shape, so the real client code — request, parse, cache,
# stale-value fallback — is what runs. KNOBS is mutable: a test sets a dial and
# the next fetch sees it, exactly as a conductor restart-free knob change does.

KNOBS: dict = {"usage_window_h": 5.0, "usage_budget_tokens": 1_000_000,
               "repair_idle_share": 0.6, "repair_yield_quiet_s": 900}
tuning_calls: list[str] = []


def _tuning_answer(request: httpx.Request) -> httpx.Response:
    name = request.url.params.get("name", "")
    tuning_calls.append(name)
    if name not in KNOBS:
        return httpx.Response(403, json={"detail": "not allowed"})
    return httpx.Response(200, json={"name": name, "value": KNOBS[name]})


svc.TUNING_TRANSPORT = httpx.MockTransport(_tuning_answer)
svc.KNOB_TTL = 0.0          # read through by default; the cache test sets its own


@pytest.fixture()
def clean_store():
    """An emptied meter for one test — cheaper than a fresh file, and the
    schema/backfill code ran once at load, exactly like a real boot."""
    svc.helpers.db().execute("DELETE FROM usage_rows")
    svc.helpers.db().commit()
    KNOBS.update({"usage_window_h": 5.0, "usage_budget_tokens": 1_000_000,
                  "repair_idle_share": 0.6, "repair_yield_quiet_s": 900})
    svc.KNOB_TTL = 0.0
    svc._KNOBS.clear()
    tuning_calls.clear()
    yield svc


# Handed over as fixtures rather than imported: this conftest registers as
# `usage.tests.conftest` (the package markers exist so two services' conftests
# cannot collide on the name `conftest`), so a test module cannot `from conftest
# import` it the way a top-level suite would.

@pytest.fixture()
def knobs():
    """The conductor's dials, as this suite's stub serves them. Mutate to change
    what the next fetch sees."""
    return KNOBS


@pytest.fixture()
def knob_reads():
    """Every knob name the service has asked the conductor for — how the cache
    tests tell 'answered from memory' from 'asked again'."""
    return tuning_calls
