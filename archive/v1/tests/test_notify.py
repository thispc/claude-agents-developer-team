"""Notifications: the difference between unattended and abandoned.

Every fault used to land in the events table, visible only to someone already
watching the feed. Walk away for a night and the first sign of trouble was that
nothing got done.

Since P2 the dedup, the hourly ceiling and the GitHub call are a SERVICE
(services/notify), mounted in-process by tests/conftest.py. These tests drive the
conductor's door — `app.notify` — over the real client path against the real
service, so what they prove is what the platform actually does. Two things moved
with the code and are tested where they now live:

  * the fingerprint's coarseness, the per-fault dedup and the hourly ceiling as
    STORE behaviour → services/notify/tests/test_notify_smoke.py
  * every degraded shape, and the /internal/bus door the service announces
    through → tests/test_notify_service.py

What stays here is what stayed in the conductor: the sprint digest (it formats
projects and tasks), the client-error route, and the dashboard's own reporting.
"""

import pytest

from app import db, notify
from conftest import notify_service
from conftest import dashboard_js  # the split dashboard JS, concatenated in load order


def _wipe_notify_store():
    con = notify_service.helpers.db()
    con.execute("DELETE FROM notify_seen")
    con.execute("DELETE FROM notify_sent")
    con.commit()


@pytest.fixture(autouse=True)
def _clean():
    """The dedup memory and the hourly counter live in the service's database
    now, so that is what gets emptied — `notify.forget()` alone would leave the
    ceiling counting yesterday's issues."""
    _wipe_notify_store()
    yield
    _wipe_notify_store()


@pytest.fixture()
def files(monkeypatch):
    """The GitHub call, captured. Patched on the SERVICE, which is the side that
    holds the token and makes the request."""
    filed = []

    async def fake_issue(repo, title, body):
        filed.append({"repo": repo, "title": title, "body": body})
        return 100 + len(filed)

    monkeypatch.setattr(notify_service, "create_issue", fake_issue)
    monkeypatch.setattr(notify, "_repo", lambda: "o/r")
    return filed


# ---- one issue per fault, not one per occurrence -------------------------

async def test_the_same_fault_is_filed_once(files):
    """A crash loop produces the same fault a thousand times. Filing a thousand
    issues is a denial of service against your own inbox."""
    for _ in range(25):
        await notify.report_error("manager crashed", "RuntimeError: boom\nline 2")
    assert len(files) == 1


async def test_repeats_are_counted(files, monkeypatch):
    async def _noop_comment(repo, number, body):
        return None
    monkeypatch.setattr(notify_service, "comment_issue", _noop_comment)
    for _ in range(5):
        r = await notify.report_error("k", "same thing")
    assert r["count"] == 5 and r["issue"] == 101


async def test_different_faults_are_different_issues(files):
    await notify.report_error("a", "first problem")
    await notify.report_error("b", "second problem")
    assert len(files) == 2


async def test_the_repo_rides_the_call_because_the_conductor_is_what_knows_it(files):
    """Which repository this platform's own faults belong to is derived from the
    git remote — conductor knowledge. Duplicating it into the service's env would
    be a second place to get it wrong."""
    await notify.report_error("k", "a problem")
    assert files[0]["repo"] == "o/r"
    import inspect
    assert '"repo": _repo()' in inspect.getsource(notify.report_error)


# ---- it must not be able to flood ---------------------------------------

async def test_there_is_a_ceiling_per_hour(files, monkeypatch):
    """If something breaks in a way we did not anticipate, the failure mode must
    be silence, not an unbounded write loop against a token that can push code."""
    monkeypatch.setattr(notify_service, "MAX_PER_HOUR", 3)
    for i in range(10):
        await notify.report_error(f"kind{i}", f"distinct problem {i}")
    assert len(files) == 3


async def test_a_broken_notifier_never_breaks_the_caller(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("github is down")
    monkeypatch.setattr(notify_service, "create_issue", boom)
    monkeypatch.setattr(notify, "_repo", lambda: "o/r")
    r = await notify.report_error("k", "something")
    assert r["sent"] is False and "could not file" in r["reason"]


async def test_no_repo_means_no_crash(monkeypatch):
    monkeypatch.setattr(notify, "_repo", lambda: "")
    r = await notify.report_error("k", "x")
    assert r["sent"] is False


# ---- the sprint digest ---------------------------------------------------
#
# It did NOT move: every line of it is a JOIN over projects and tasks, and
# handing a service a reader on those would undo the isolation the extraction
# bought. It composes the text here and posts the finished issue through the
# service's generic door.

async def test_a_sprint_digest_says_what_shipped(fresh_db, monkeypatch):
    """"Six sprints ran overnight, here is what came out" is the entire reason
    for asking for six sprints."""
    body = {}

    async def fake_issue(repo, title, b):
        body["title"], body["text"] = title, b
        return 7
    monkeypatch.setattr(notify_service, "create_issue", fake_issue)

    p = db.create_project("shop", "b", "o/r", 5.0, 3, owner_id=1, sprints=3)
    db.set_project_status(p, "running")
    a = db.create_task(p, "backend", "checkout endpoint", "d")
    db.update_task(a, status="done", verification='{"ran": true, "ok": true}')
    b2 = db.create_task(p, "tester", "e2e pass", "d")
    db.update_task(b2, status="failed", verification='{"ran": true, "ok": false}')

    r = await notify.sprint_digest(p, 1)
    assert r["sent"] and "1 shipped, 1 failed" in body["title"]
    assert "checkout endpoint" in body["text"] and "e2e pass" in body["text"]


async def test_the_digest_flags_unverified_work(fresh_db, monkeypatch):
    """A project with no test command produces work nothing checked. That is the
    single most important caveat on any 'shipped' claim."""
    body = {}

    async def fake_issue(repo, title, b):
        body["text"] = b
        return 7
    monkeypatch.setattr(notify_service, "create_issue", fake_issue)
    p = db.create_project("x", "b", "o/r", 5.0, 3, owner_id=1, sprints=2)
    t = db.create_task(p, "backend", "a thing", "d")
    db.update_task(t, status="done")          # no verification at all
    await notify.sprint_digest(p, 1)
    assert "unverified" in body["text"]


async def test_the_digest_is_not_deduplicated_into_silence(fresh_db, monkeypatch):
    """It goes through the generic /issue door precisely because it must not be
    fingerprinted: two sprints with the same headline are two sprints, and the
    second one still happened."""
    filed = []

    async def fake_issue(repo, title, b):
        filed.append(title)
        return len(filed)
    monkeypatch.setattr(notify_service, "create_issue", fake_issue)
    p = db.create_project("x", "b", "o/r", 5.0, 3, owner_id=1, sprints=2)
    await notify.sprint_digest(p, 1)
    await notify.sprint_digest(p, 1)
    assert len(filed) == 2


# ---- the browser --------------------------------------------------------

def test_the_dashboard_reports_its_own_errors():
    js = dashboard_js()
    assert "/api/client-error" in js
    assert "unhandledrejection" in js, "a rejected promise is the commonest silent failure"
    assert "_reported" in js, "it must not report the same error on every render"


def test_client_error_route_exists(root_client, fresh_db, monkeypatch):
    async def fake(kind, detail, ctx):
        return {"sent": True, "issue": 1}
    monkeypatch.setattr("app.routes.notify.report_error", fake)
    r = root_client.post("/api/client-error", json={"message": "x is not a function"})
    assert r.status_code == 200


# ---- credentials must actually rotate -----------------------------------

def test_changing_the_root_password_actually_changes_it(fresh_db, monkeypatch):
    """It used to do nothing: root was seeded on first boot and never touched
    again, so rotating the secret left the old password working and the new one
    rejected, with no error anywhere to say so."""
    from app import auth
    assert auth.verify(auth.ROOT_USERNAME, "testpass")

    monkeypatch.setattr(auth, "ROOT_PASSWORD", "a-much-longer-new-password")
    auth.init()
    assert auth.verify(auth.ROOT_USERNAME, "a-much-longer-new-password"), "rotation did nothing"
    assert not auth.verify(auth.ROOT_USERNAME, "testpass"), "the old password still works"


def test_login_locks_out_after_repeated_failures(fresh_db):
    """Unbounded guessing is only survivable behind a firewall. The moment this is
    reachable from the internet the password IS the security boundary."""
    from app import auth
    auth.clear_lockouts()
    for _ in range(auth.LOCKOUT_AFTER):
        auth.verify("root", "nope")
    assert auth.locked_out("root") > 0
    assert auth.verify("root", "testpass") is None, "correct password accepted while locked"
    auth.clear_lockouts()


def test_a_successful_login_clears_the_counter(fresh_db):
    from app import auth
    auth.clear_lockouts()
    auth.verify("root", "nope")
    assert auth.verify("root", "testpass")
    assert auth.locked_out("root") == 0
    auth.clear_lockouts()


def test_guessing_usernames_is_not_free(fresh_db):
    """Otherwise 'that account does not exist' is itself information worth having."""
    from app import auth
    auth.clear_lockouts()
    for _ in range(auth.LOCKOUT_AFTER):
        auth.verify("does-not-exist", "x")
    assert auth.locked_out("does-not-exist") > 0
    auth.clear_lockouts()
