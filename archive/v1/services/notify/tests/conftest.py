"""Test env for the notify service, set BEFORE app/helpers latch their config.

Offline by construction: a temp database, a known token, no legacy db, no
conductor, no GitHub and no sockets — the suite drives the ASGI app in-process
and the git host is monkeypatched per test.

Two departures from the template's conftest, both because this suite must also
run INSIDE the full repo run (pytest.ini's testpaths include services/):

  1. The service is loaded under UNIQUE module names ("notify_service_app",
     "notify_service_helpers") via importlib, because the conductor's `app`
     package is already in sys.modules when tests/ collected first — a plain
     `from app import app` would hit the wrong one. Test files fetch the loaded
     modules with a plain `import notify_service_app`, which resolves from
     sys.modules (this conftest always imports before its test files).

  2. Every env var this file touches is SAVED and RESTORED after the load,
     because helpers/app read the environment at import time and the conductor
     suite's conftest pinned its own values — leaking DB_PATH across suites is
     how one suite's fixture deletes another suite's database.

GITHUB_TOKEN is set to a fake so `gh_enabled()` is true and the drills reach the
(monkeypatched) call. Nothing here ever opens a socket.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parent.parent
_TMP = tempfile.mkdtemp(prefix="notify-svc-test-")

TOKEN = "test-service-token"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_ENV = {k: os.environ.get(k)
        for k in ("DB_PATH", "SERVICE_TOKEN", "SERVICE_NAME", "LEGACY_DB_PATH",
                  "CONDUCTOR_URL", "GITHUB_TOKEN", "NOTIFY_GITHUB",
                  "NOTIFY_MAX_PER_HOUR")}
os.environ["DB_PATH"] = str(Path(_TMP) / "notify-test.db")
os.environ["SERVICE_TOKEN"] = TOKEN
os.environ["SERVICE_NAME"] = "notify"
os.environ["LEGACY_DB_PATH"] = str(Path(_TMP) / "absent-legacy.db")   # no first-boot copy
os.environ["CONDUCTOR_URL"] = "http://conductor.test"
os.environ["GITHUB_TOKEN"] = "test-github-token-not-real"
os.environ["NOTIFY_GITHUB"] = "1"
os.environ["NOTIFY_MAX_PER_HOUR"] = "6"
_SAVED_HELPERS = sys.modules.pop("helpers", None)
# app.py puts its own directory first on sys.path (so `uvicorn app:app` works from
# any cwd); left there it would shadow the conductor's `app` package for whatever
# runs next in a whole-repo pytest run.
_SAVED_PATH = list(sys.path)
try:
    helpers = _load("notify_service_helpers", SERVICE_DIR / "helpers.py")
    # app.py's own `import helpers` must find THIS configured instance, not a
    # fresh one latched to someone else's environment.
    sys.modules["helpers"] = helpers
    svc = _load("notify_service_app", SERVICE_DIR / "app.py")
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


@pytest.fixture()
def clean_store():
    """An emptied notifier for one test: the dedup memory AND the hourly counter.
    Forgetting the counter is how a suite starts throttling itself halfway
    through."""
    con = svc.helpers.db()
    con.execute("DELETE FROM notify_seen")
    con.execute("DELETE FROM notify_sent")
    con.commit()
    svc.MAX_PER_HOUR = 6
    svc.ENABLED = True
    yield svc


@pytest.fixture()
def filed(monkeypatch, clean_store):
    """The GitHub call, captured. Nothing in this suite ever reaches the network."""
    out = []

    async def fake_issue(repo, title, body):
        out.append({"repo": repo, "title": title, "body": body})
        return 100 + len(out)

    async def fake_comment(repo, number, body):
        out.append({"comment_on": number, "body": body})

    monkeypatch.setattr(svc, "create_issue", fake_issue)
    monkeypatch.setattr(svc, "comment_issue", fake_comment)
    return out
