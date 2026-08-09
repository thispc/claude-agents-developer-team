"""Smoke: the contract's guarantees plus the two rules that make a notifier
survivable rather than a spam machine.

In-process and offline. What the CONDUCTOR does with the answers — the degraded
shape, the sprint digest, the bus door — lives in tests/test_notify_service.py;
this file proves the SERVICE: auth, shapes, the committed spec, dedup, the
hourly ceiling, and the first-boot copy of the conductor's dedup memory.
"""

import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

import notify_service_app as svc      # loaded by conftest under a unique name

SERVICE_DIR = Path(__file__).resolve().parent.parent
TOKEN = {"X-Service-Token": "test-service-token"}

client = TestClient(svc.app)


def _report(kind="k", detail="something broke", **extra):
    return client.post("/error", json={"kind": kind, "detail": detail,
                                       "repo": "o/r", **extra}, headers=TOKEN).json()


# --- the contract -------------------------------------------------------------

def test_health_is_the_contracts_readiness_shape():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"ok", "service", "db", "checks"}
    assert body["ok"] is True and body["checks"]["db"] is True
    assert body["checks"]["table"] is True
    assert body["service"] == "notify"


def test_health_and_the_spec_need_no_token_but_every_verb_does():
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    for method, path, body in (("post", "/error", {"kind": "k", "detail": "d"}),
                               ("post", "/issue", {"repo": "o/r", "title": "t"}),
                               ("get", "/status", None),
                               ("post", "/forget", {})):
        r = getattr(client, method)(path, json=body) if body is not None \
            else client.get(path)
        assert r.status_code == 401, f"{path} answered {r.status_code} without a token"
        r = getattr(client, method)(path, json=body, headers={"X-Service-Token": "wrong"}) \
            if body is not None else client.get(path, headers={"X-Service-Token": "wrong"})
        assert r.status_code == 401, f"{path} accepted a wrong token"


def test_the_served_spec_is_the_committed_file_byte_for_byte():
    committed = json.loads((SERVICE_DIR / "openapi.json").read_text())
    assert client.get("/openapi.json").json() == committed


# --- one issue per distinct fault ---------------------------------------------

def test_the_fingerprint_ignores_line_noise():
    """Stack traces differ by line numbers between otherwise identical crashes; a
    finer fingerprint would file a fresh issue for every one."""
    a = svc.fingerprint("k", "RuntimeError: boom\n  at line 41\n  at 0x7f")
    b = svc.fingerprint("k", "RuntimeError: boom\n  at line 88\n  at 0x9c")
    assert a == b
    assert svc.fingerprint("other", "RuntimeError: boom\n  at line 41") != a


def test_the_same_fault_is_filed_once_and_counted_after(filed):
    """A crash loop produces the same fault a thousand times. Filing a thousand
    issues is a denial of service against your own inbox."""
    first = _report(detail="RuntimeError: boom\nline 2")
    assert first == {"sent": True, "issue": 101}
    for _ in range(24):
        again = _report(detail="RuntimeError: boom\nline 2")
    assert len([f for f in filed if "title" in f]) == 1
    assert again["sent"] is False and again["count"] == 25 and again["issue"] == 101


def test_a_repeat_comments_only_on_milestones(filed):
    for _ in range(10):
        _report(detail="the same thing")
    comments = [f for f in filed if "comment_on" in f]
    assert len(comments) == 1 and comments[0]["comment_on"] == 101
    assert "10 times" in comments[0]["body"]


def test_the_count_is_incremented_in_sql_not_read_then_written(filed):
    """The whole reason this state left the conductor's kv blob: two processes
    counting the same crash loop must not each write '2'."""
    body = Path(svc.__file__).read_text().split("async def report_error(", 1)[1] \
                                         .split("\nasync def ", 1)[0]
    assert "SET count=count+1" in body


def test_different_faults_are_different_issues(filed):
    _report(kind="a", detail="first problem")
    _report(kind="b", detail="second problem")
    assert len([f for f in filed if "title" in f]) == 2


def test_a_throttled_fault_is_still_remembered(filed):
    """Otherwise the ceiling turns into an amnesia: the fault is dropped AND
    forgotten, so the next hour files it as brand new."""
    svc.MAX_PER_HOUR = 1
    _report(kind="a", detail="one")
    second = _report(kind="b", detail="two")
    assert second["sent"] is False and "in an hour" in second["reason"]
    assert svc._seen(svc.fingerprint("b", "two")) is not None


# --- the ceiling --------------------------------------------------------------

def test_there_is_a_ceiling_per_hour(filed):
    """If something breaks in a way we did not anticipate, the failure mode must
    be silence, not an unbounded write loop against a token that can push code."""
    svc.MAX_PER_HOUR = 3
    for i in range(10):
        _report(kind=f"kind{i}", detail=f"distinct problem {i}")
    assert len([f for f in filed if "title" in f]) == 3


def test_the_ceiling_is_a_rolling_hour_not_a_lifetime(filed):
    svc.MAX_PER_HOUR = 1
    _report(kind="a", detail="one")
    # the first issue was filed 61 minutes ago
    svc.helpers.db().execute("UPDATE notify_sent SET ts = ?", (time.time() - 3700,))
    svc.helpers.db().commit()
    assert _report(kind="b", detail="two")["sent"] is True


def test_the_generic_door_respects_the_ceiling_too(filed):
    """The ceiling exists to protect the token, not the inbox — so a caller that
    composes its own text does not get a way around it."""
    svc.MAX_PER_HOUR = 1
    assert client.post("/issue", json={"repo": "o/r", "title": "one", "body": "b"},
                       headers=TOKEN).json()["sent"] is True
    out = client.post("/issue", json={"repo": "o/r", "title": "two", "body": "b"},
                      headers=TOKEN).json()
    assert out["sent"] is False and "in an hour" in out["reason"]


def test_the_generic_door_does_not_deduplicate(filed):
    """Two sprints with the same headline are two sprints, and the second one
    still happened."""
    for _ in range(3):
        client.post("/issue", json={"repo": "o/r", "title": "Sprint 1 digest",
                                    "body": "b"}, headers=TOKEN)
    assert len([f for f in filed if "title" in f]) == 3


# --- never break the thing it is reporting on ---------------------------------

def test_a_broken_git_host_is_a_reason_not_an_exception(clean_store, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("github is down")
    monkeypatch.setattr(svc, "create_issue", boom)
    out = _report()
    assert out["sent"] is False and "could not file" in out["reason"]


def test_no_repo_and_no_token_each_refuse_quietly(clean_store, monkeypatch):
    assert client.post("/error", json={"kind": "k", "detail": "d", "repo": ""},
                       headers=TOKEN).json()["sent"] is False
    monkeypatch.setattr(svc, "GITHUB_TOKEN", "")
    assert _report(kind="other")["sent"] is False


def test_the_kill_switch_stops_everything(clean_store, monkeypatch):
    monkeypatch.setattr(svc, "ENABLED", False)
    assert _report()["reason"] == "disabled"
    assert client.post("/issue", json={"repo": "o/r", "title": "t", "body": "b"},
                       headers=TOKEN).json()["reason"] == "disabled"


# --- status and forget --------------------------------------------------------

def test_status_says_what_has_been_reported(filed):
    _report(kind="crash", detail="RuntimeError: boom")
    st = client.get("/status", headers=TOKEN).json()
    assert st["enabled"] is True and st["sent_last_hour"] == 1
    assert st["max_per_hour"] == svc.MAX_PER_HOUR
    (fault,) = st["distinct_faults"]
    assert fault["count"] == 1 and fault["issue"] == 101
    assert fault["kind"] == "crash" and "RuntimeError" in fault["head"]


def test_forgetting_one_fault_makes_a_recurrence_loud_again(filed):
    _report(kind="crash", detail="boom")
    fp = svc.fingerprint("crash", "boom")
    assert client.post("/forget", json={"fingerprint": fp}, headers=TOKEN).json() \
        == {"removed": 1}
    assert _report(kind="crash", detail="boom")["sent"] is True
    assert len([f for f in filed if "title" in f]) == 2


def test_forgetting_everything_takes_the_whole_memory(filed):
    _report(kind="a", detail="one")
    _report(kind="b", detail="two")
    assert client.post("/forget", json={}, headers=TOKEN).json()["removed"] == 2
    assert client.get("/status", headers=TOKEN).json()["distinct_faults"] == []


def test_the_memory_survives_the_services_own_restart(filed):
    """The fault most worth deduplicating is the one that kills the process. This
    is the store's half of that promise: reopen the file, the count is still
    there."""
    _report(kind="crash", detail="Traceback: boom")
    con = svc.helpers._conn
    svc.helpers._conn = None
    con.close()
    assert _report(kind="crash", detail="Traceback: boom")["count"] == 2
    assert len([f for f in filed if "title" in f]) == 1


# --- first boot: the conductor's dedup memory comes across --------------------

def test_first_boot_copies_the_conductors_dedup_memory(tmp_path, monkeypatch, clean_store):
    """Without it the extraction itself becomes a notification storm: every fault
    already filed would look new again on the first boot after the cutover."""
    p = tmp_path / "devteam.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL)")
    con.execute("INSERT INTO kv (k, v, ts) VALUES ('notify_seen:abc123', ?, 0)",
                (json.dumps({"count": 3, "first": 1000.0, "last": 2000.0, "issue": 12}),))
    con.execute("INSERT INTO kv (k, v, ts) VALUES ('notify_sent', '[1,2,3]', 0)")
    con.commit()
    con.close()

    monkeypatch.setattr(svc, "LEGACY_DB_PATH", p)
    svc.helpers.db().execute("DELETE FROM kv WHERE key='backfilled_from'")
    svc.helpers.db().commit()
    assert svc.backfill_from_legacy() == 1
    row = svc._seen("abc123")
    assert row["count"] == 3 and row["issue"] == 12
    # the hourly counter is deliberately NOT copied: it is a sliding window that
    # empties within the hour, and starting at zero errs toward being able to
    # tell you something
    assert svc._sent_last_hour() == 0
    assert svc.backfill_from_legacy() == 0          # exactly once


def test_no_legacy_database_is_not_an_error(tmp_path, monkeypatch, clean_store):
    monkeypatch.setattr(svc, "LEGACY_DB_PATH", tmp_path / "nothing-here.db")
    svc.helpers.db().execute("DELETE FROM kv WHERE key='backfilled_from'")
    svc.helpers.db().commit()
    assert svc.backfill_from_legacy() == 0
    assert client.get("/health").json()["ok"] is True


# --- the credential stays where it was put ------------------------------------

def test_this_service_holds_a_git_token_and_no_model_credential():
    """The first credential beyond the conductor, and the contract's one
    documented exception. It must not become a second one."""
    src = Path(svc.__file__).read_text()
    assert "GITHUB_TOKEN" in src
    for forbidden in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                      "GEMINI_API_KEY"):
        assert forbidden not in src, f"{forbidden} has no business in this service"
