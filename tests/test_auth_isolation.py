"""Feature: per-user isolation + the 11 route-authorization fixes + WS/worker auth.

Commits: 077e73b (per-user isolation), 6b4c029 (close 11 unauthorized routes,
credential leak, worker token). This is the security surface, so it gets the
most tests.
"""

import pytest

from conftest import make_project, make_task
from app import db


# Every project/task-scoped route that a non-owner must not reach.
# (method, path-template) — {pid}/{tid}/{qid} filled per test.
PROJECT_ROUTES = [
    ("post", "/api/projects/{pid}/restart"),
    ("post", "/api/projects/{pid}/cancel"),
    ("post", "/api/projects/{pid}/preview"),
    ("get", "/api/projects/{pid}/events"),
    ("get", "/api/projects/{pid}/artifacts"),
    ("get", "/api/projects/{pid}/blockers"),
    ("get", "/api/projects/{pid}/deploy"),
    ("get", "/api/projects/{pid}/question"),
    ("post", "/api/projects/{pid}/directive"),
    ("post", "/api/projects/{pid}/budget"),
    ("post", "/api/projects/{pid}/tasks"),
]
TASK_ROUTES = [
    ("get", "/api/tasks/{tid}/events"),
    ("get", "/api/tasks/{tid}/machine-logs"),
    ("post", "/api/tasks/{tid}/retry"),
    ("post", "/api/tasks/{tid}/skip"),
    ("post", "/api/tasks/{tid}/edit"),
    ("post", "/api/tasks/{tid}/kill"),
]

# Bodies so the request reaches the auth check rather than failing validation.
BODIES = {
    "/api/projects/{pid}/directive": {"text": "x"},
    "/api/projects/{pid}/budget": {"budget_usd": 9999},
    "/api/projects/{pid}/tasks": {"role": "backend", "title": "x", "description": "y"},
    "/api/tasks/{tid}/edit": {"title": "hijacked"},
}


def _call(c, method, path, **fmt):
    body = BODIES.get(path, {})
    url = path.format(**fmt)
    fn = getattr(c, method)
    if method == "get":
        return fn(url)                       # GET takes no json body
    return fn(url, json=body.copy() if body else {})


def test_owner_sees_only_their_projects(root_client, make_user):
    make_project(owner_id=1, name="root-proj")
    uid, u_client = make_user("alice")
    make_project(owner_id=uid, name="alice-proj")

    # root sees both (root is operator)
    assert len(root_client.get("/api/projects").json()) == 2
    # alice sees only her own
    alice = u_client.get("/api/projects").json()
    assert [p["name"] for p in alice] == ["alice-proj"]


@pytest.mark.parametrize("method,path", PROJECT_ROUTES)
def test_project_routes_reject_anonymous(client, method, path):
    pid = make_project(owner_id=1)
    r = _call(client, method, path, pid=pid)
    assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"


@pytest.mark.parametrize("method,path", PROJECT_ROUTES)
def test_project_routes_reject_other_user(client, make_user, method, path):
    pid = make_project(owner_id=1, name="root-proj")   # owned by root
    _uid, u_client = make_user("mallory")
    r = _call(u_client, method, path, pid=pid)
    # 404, not 403 — existence must not leak
    assert r.status_code == 404, f"{method} {path} -> {r.status_code} (want 404)"


@pytest.mark.parametrize("method,path", TASK_ROUTES)
def test_task_routes_reject_other_user(client, make_user, method, path):
    pid = make_project(owner_id=1)
    tid = make_task(pid)
    _uid, u_client = make_user("eve")
    r = _call(u_client, method, path, tid=tid)
    assert r.status_code == 404, f"{method} {path} -> {r.status_code} (want 404)"


def test_owner_can_use_their_own_routes(root_client):
    pid = make_project(owner_id=1)
    assert root_client.get(f"/api/projects/{pid}/blockers").status_code == 200
    assert root_client.post(f"/api/projects/{pid}/directive",
                            json={"text": "hi"}).status_code == 200


def test_worker_token_required_for_internal(client):
    pid = make_project(owner_id=1)
    tid = make_task(pid)
    # no token
    r = client.post("/internal/report", json={
        "project_id": pid, "task_id": tid, "status": "pushed", "report": "x"})
    assert r.status_code == 401
    # wrong token
    r = client.post("/internal/report",
                    headers={"X-Worker-Token": "wrong"},
                    json={"project_id": pid, "task_id": tid, "status": "pushed", "report": "x"})
    assert r.status_code == 401


def test_worker_cannot_forge_cross_project_report(client):
    """A valid token must not let a worker report on a task from another project."""
    p1 = make_project(owner_id=1, name="p1")
    p2 = make_project(owner_id=1, name="p2")
    t2 = make_task(p2)   # task belongs to p2
    r = client.post("/internal/report",
                    headers={"X-Worker-Token": "test-worker-token"},
                    json={"project_id": p1, "task_id": t2, "status": "pushed", "report": "forged"})
    assert r.status_code == 400  # task does not belong to that project


def test_valid_worker_report_is_accepted(client):
    pid = make_project(owner_id=1)
    tid = make_task(pid, status="running")
    r = client.post("/internal/report",
                    headers={"X-Worker-Token": "test-worker-token"},
                    json={"project_id": pid, "task_id": tid, "status": "pushed", "report": "ok"})
    assert r.status_code == 200
    assert db.get_task(tid)["status"] == "pushed"


def test_websocket_rejects_anonymous(client):
    from starlette.testclient import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()


def test_signup_user_has_no_credentials_and_cannot_inherit(client, make_user):
    uid, _ = make_user("newbie")
    from app import auth
    u = auth.get_user(uid)
    assert auth.has_own_ai_credentials(u) is False
    # owner_credentials must return a blanked env, never the operator's
    from app import launcher
    creds = launcher.owner_credentials({"owner_id": uid})
    assert creds.get("ANTHROPIC_API_KEY", "") == ""
    assert creds.get("CLAUDE_CODE_OAUTH_TOKEN", "") == ""
    # and it must actively blank the inherited vars, not just omit them
    assert "ANTHROPIC_API_KEY" in creds
    assert "CLAUDE_CODE_OAUTH_TOKEN" in creds
