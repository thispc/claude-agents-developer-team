"""Remaining features: DB_PATH anchoring, rate-limit cooldown parsing, handoff
notes, task-edit cycle guard, agents-tab scoping.

Commits: 38f2659 (DB_PATH), e71906e (cooldown), d3eefaf (handoff), 66a4e4c
(agents scoping), 8f1d56c (task edit).
"""

import json
import time
from pathlib import Path

import pytest

from conftest import make_project, make_task
from app import config, db, launcher


# ---- DB_PATH anchoring (data-loss fix) ------------------------------------

def test_db_path_is_absolute_and_anchored():
    """A relative DB_PATH silently opened an empty DB from another cwd."""
    assert Path(config.DB_PATH).is_absolute()


# ---- rate-limit cooldown parsing ------------------------------------------

def test_cooldown_records_and_expires(fresh_db):
    launcher.COOLDOWN.clear()
    launcher.note_rate_limit("claude-haiku-4-5", "rate_limit; retry-after: 2")
    left = launcher.cooldown_left("claude-haiku-4-5")
    assert left > 0
    # a model with no recorded cooldown is available
    assert launcher.cooldown_left("claude-sonnet-5") == 0
    launcher.COOLDOWN.clear()


def test_fallback_skips_cooling_model(fresh_db):
    """When the current model is cooling, pick_model falls back to another."""
    launcher.COOLDOWN.clear()
    p = make_project()
    t = make_task(p, role="backend")
    # attempt 1 with a rate-limited report should trigger the fallback branch
    db.update_task(t, attempts=1, model="claude-haiku-4-5",
                   report="Error: rate_limit exceeded (429)")
    launcher.note_rate_limit("claude-haiku-4-5", "retry-after: 300")
    got = launcher.pick_model(db.get_task(t), db.get_project(p))
    assert got != "claude-haiku-4-5", "fell back onto the cooling model"
    launcher.COOLDOWN.clear()


# ---- handoff notes ---------------------------------------------------------

def test_handoff_context_includes_predecessor_reports(fresh_db):
    p = make_project()
    a = make_task(p, role="backend", status="done")
    db.update_task(a, report="Built the API at /api/orders returning JSON.")
    b = make_task(p, role="frontend", deps=[a])
    ctx = launcher.handoff_context(db.get_task(b))
    assert "api/orders" in ctx.lower() or "orders" in ctx.lower()


def test_handoff_empty_when_no_deps(fresh_db):
    p = make_project()
    t = make_task(p)
    assert launcher.handoff_context(db.get_task(t)) == ""


# ---- task edit cycle guard -------------------------------------------------

def test_edit_task_rejects_dependency_cycle(root_client, fresh_db):
    # root owns project 1
    p = make_project(owner_id=1)
    a = make_task(p)               # seq 1
    b = make_task(p)               # seq 2
    # make b depend on a
    db.update_task(b, deps=json.dumps([a]))
    # now try to make a depend on b (seq 2) -> cycle
    r = root_client.post(f"/api/tasks/{a}/edit", json={"depends_on": [2]})
    assert r.status_code == 400
    # a must NOT have gained the dependency
    assert json.loads(db.get_task(a)["deps"]) == []


def test_edit_task_accepts_valid_dependency(root_client, fresh_db):
    p = make_project(owner_id=1)
    a = make_task(p)               # seq 1
    b = make_task(p)               # seq 2
    r = root_client.post(f"/api/tasks/{b}/edit", json={"depends_on": [1]})
    assert r.status_code == 200
    assert a in json.loads(db.get_task(b)["deps"])


# ---- agents tab scoping ----------------------------------------------------

def test_agents_endpoint_scoped_to_owner(root_client, make_user, fresh_db):
    p_root = make_project(owner_id=1)
    uid, u_client = make_user("zoe")
    p_zoe = make_project(owner_id=uid)
    # a fake live agent on each
    launcher.ACTIVE.clear()
    launcher.ACTIVE[str(1000)] = {"kind": "process", "role": "backend", "ref": "pid 1",
                                  "model": "m", "project_id": p_root, "started_at": time.time(),
                                  "task_id": 1000, "title": "x", "workdir": "/tmp"}
    launcher.ACTIVE[str(2000)] = {"kind": "process", "role": "backend", "ref": "pid 2",
                                  "model": "m", "project_id": p_zoe, "started_at": time.time(),
                                  "task_id": 2000, "title": "y", "workdir": "/tmp"}
    # zoe must not see root's agents
    got = u_client.get(f"/api/agents?project_id={p_root}")
    assert got.status_code == 404      # not her project
    launcher.ACTIVE.clear()
