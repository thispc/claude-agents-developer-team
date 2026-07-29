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


# ---- the activity feed must show RECENT events, not the oldest ones --------

def test_list_events_returns_the_newest_not_the_oldest(fresh_db):
    """With ORDER BY id LIMIT 500 this returned the OLDEST 500, so any project
    past 500 events froze on ancient history and never showed anything current."""
    p = make_project()
    for i in range(30):
        db.add_event(p, None, "system", "tick", {"n": i})
    got = db.list_events(p, limit=10)
    assert len(got) == 10
    # newest ten, still in chronological order
    payloads = [json.loads(e["payload"])["n"] for e in got]
    assert payloads == list(range(20, 30)), payloads


def test_list_events_tailing_still_returns_what_is_new(fresh_db):
    p = make_project()
    ids = [db.add_event(p, None, "system", "tick", {"n": i})["id"] for i in range(10)]
    tail = db.list_events(p, after_id=ids[4])
    assert [json.loads(e["payload"])["n"] for e in tail] == [5, 6, 7, 8, 9]


# ---- all-day unattended running -------------------------------------------

def test_cooldowns_survive_a_restart(fresh_db):
    """In-memory only, every restart forgot which models were throttled and
    dispatched straight back onto them."""
    launcher.COOLDOWN.clear()
    launcher.note_rate_limit("claude-haiku-4-5", "rate_limit; retry-after: 120")
    launcher.COOLDOWN.clear()                 # simulate the process dying
    assert launcher.load_cooldowns() == 1
    assert launcher.cooldown_left("claude-haiku-4-5") > 60
    db._execute("DELETE FROM model_cooldown")
    launcher.COOLDOWN.clear()


def test_all_models_cooling_holds_the_task_instead_of_burning_an_attempt(fresh_db):
    """An overnight run should ride out a limit, not spend attempts against it."""
    import asyncio, time as _t
    launcher.COOLDOWN.clear()
    for m in launcher.FALLBACK_ORDER:
        launcher.COOLDOWN[m] = _t.time() + 90
    p = make_project(owner_id=1)
    t = make_task(p)
    out = asyncio.run(launcher.dispatch_task(t))
    assert "waiting" in out and "rate limited" in out
    assert db.get_task(t)["status"] == "planned"     # still dispatchable later
    assert db.get_task(t)["attempts"] == 0           # attempt not consumed
    launcher.COOLDOWN.clear()


def test_autonomous_grace_is_short_enough_for_an_overnight_run():
    """A full-autonomy manager must not lose an hour to a sleeping boss."""
    assert config.AUTONOMOUS_QUESTION_GRACE <= 900


# ---- the settings dialog must not silently drop credentials ---------------
# The Save handler named three fields by hand, so the OpenAI and Gemini inputs
# were decorative: paste a key, get a success toast, key never stored.

import re as _re

from conftest import dashboard_js  # the split dashboard JS, concatenated in load order

DASH = Path(config.__file__).resolve().parents[2] / "dashboard"


def _settings_form_inputs() -> set[str]:
    html = (DASH / "index.html").read_text()
    form = html.split('id="settingsForm"', 1)[1].split("</form>", 1)[0]
    return set(_re.findall(r'<input[^>]*\bname="([^"]+)"', form))


def test_every_settings_input_is_a_field_the_backend_accepts():
    from app.routes import Settings
    assert _settings_form_inputs() <= set(Settings.model_fields), (
        "the dialog collects a credential no route will store")


def test_every_backend_credential_has_an_input():
    from app.routes import Settings
    assert set(Settings.model_fields) <= _settings_form_inputs(), (
        "a credential the backend supports has no way to be entered")


def test_save_handler_submits_the_whole_form_not_a_hand_written_list():
    js = dashboard_js()
    handler = js.split('$("#settingsForm").addEventListener("submit"', 1)[1][:900]
    assert "f.entries()" in handler, "the handler enumerates fields by hand again"
    for dropped in ("openai_api_key", "gemini_api_key"):
        assert f'body.{dropped} =' not in handler


def test_settings_route_persists_every_credential(root_client, fresh_db):
    from app import auth
    from app.routes import Settings
    payload = {f: f"value-for-{f}" for f in Settings.model_fields}
    r = root_client.post("/api/settings", json=payload)
    assert r.status_code == 200, r.text
    stored = auth.get_settings(auth.get_user(1))
    for f in Settings.model_fields:
        assert stored.get(f) == f"value-for-{f}", f"{f} was not persisted"


# ---- artifacts: the output, not the activity -----------------------------

def test_files_endpoint_refuses_a_path_that_escapes_the_repo(root_client, fresh_db):
    from conftest import make_project as _mp
    p = _mp(owner_id=1, repo="o/r")
    assert root_client.get(f"/api/projects/{p}/file?path=../../etc/passwd").status_code == 400


def test_files_endpoint_is_owner_scoped(client, make_user, fresh_db):
    from conftest import make_project as _mp
    p = _mp(owner_id=1, repo="o/r")
    _uid, c2 = make_user("nosy")
    assert c2.get(f"/api/projects/{p}/files").status_code == 404


def test_files_says_why_when_there_is_no_repo(root_client, fresh_db):
    from conftest import make_project as _mp
    p = _mp(owner_id=1, repo="")
    d = root_client.get(f"/api/projects/{p}/files").json()
    assert d["files"] == [] and "no GitHub repo" in d["reason"]


def test_the_artifacts_page_groups_files_by_what_they_are():
    """"a README" and "a source file" are different kinds of thing to a reader,
    even though git treats them identically."""
    js = dashboard_js()
    assert "loadProjectFiles" in js and "FILE_ICON" in js
    block = js.split("async function loadProjectFiles(", 1)[1][:1200]
    for k in ("Documents", "Code", "Tests"):
        assert k in block


def test_code_is_not_reflowed_but_prose_is():
    """Wrapping code is how you make it unreadable."""
    css = (DASH / "style.css").read_text()
    assert ".file-code" in css and "white-space: pre;" in css
    assert ".file-doc" in css and "pre-wrap" in css


def test_a_project_on_hold_can_be_restarted():
    """A project on hold whose manager died is waiting for an answer nothing is
    listening for — not restartable and not advanceable, i.e. stuck forever."""
    src = (Path(config.__file__).resolve().parents[1] / "app" / "routes.py").read_text()
    assert '("failed", "review", "cancelled", "hold")' in src
