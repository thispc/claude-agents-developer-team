"""Self-repair v2 — the IT crew. Ironclad offline: the button, the state machine, the
meters/sleep math, the builder's git surgery, and the pinned UI contract — all with zero
model calls (a monkeypatched provider raises if anything tries to spend)."""

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from conftest import dashboard_js
from app import auth, config, db, launcher, repair, tuning
from app import repair_builder as rb


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    """launcher.COOLDOWN is a module global that outlives fresh_db, and a test that expires a
    cooldown's DB row does not evict the in-memory entry — which then makes the NEXT test's
    headroom() read as 'cooling'. Clear it around every test in this file."""
    launcher.COOLDOWN.clear()
    yield
    launcher.COOLDOWN.clear()


@pytest.fixture()
def no_spend(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("an offline repair test touched a provider")
    from app import providers
    monkeypatch.setattr(providers, "complete", boom)
    monkeypatch.setattr(rb, "_run_sdk", boom)
    return boom


def _root_user():
    return auth.get_user_by_name(auth.ROOT_USERNAME) or auth.create_user(
        auth.ROOT_USERNAME, "pw-root", is_root=True)


# --------------------------------------------------------------------------
# the button + state machine
# --------------------------------------------------------------------------

def test_the_button_persists_and_a_disabled_engine_does_nothing(fresh_db, no_spend):
    assert repair.enabled() is False
    repair.toggle(True)
    assert repair.enabled() is True and db.kv_get("repair:enabled") is True
    repair.toggle(False)
    calls = []
    orig = repair.advance
    repair.advance = lambda st: calls.append(st)          # would be awaited if reached
    try:
        asyncio.run(repair.tick())
    finally:
        repair.advance = orig
    assert calls == [], "a disabled engine must not advance"


def test_an_offline_sprint_completes_deterministically_with_zero_spend(fresh_db, no_spend, monkeypatch):
    """The whole loop, free: enable → scout(offline) → plan(free deliberation, no tasks)
    → retro → resting. The crew world is real and visible; nothing spends."""
    _root_user()
    monkeypatch.setattr(config, "AUTH_CONFIGURED", False)
    repair.toggle(True)
    for _ in range(8):
        asyncio.run(repair.tick())
        if repair.state()["phase"] == "sleeping":
            break
    st = repair.state()
    assert st["phase"] == "sleeping" and "resting" in st["sleep_reason"]
    rec = repair.sprint(1)
    assert rec and rec["scout"]["digest"].startswith("(offline")
    assert rec["tasks"] == [] and "nothing worth doing" in rec["retro"]
    info = repair.team()
    assert info and info["world_id"] and len(info["agents"]) == len(repair.enabled_factors())
    assert db.kv_get("repair:seq") == 1


def test_only_one_engine_process_may_drive_a_database(fresh_db, no_spend, monkeypatch):
    """The sprint-2 lesson: a forgotten server from an old session kept ticking against the
    same devteam.db, so TWO engines planned one sprint and orphaned a green branch. A kv lease
    (pid + heartbeat) means the second process stands down; a dead holder's lease expires."""
    import os
    import time as _t
    assert repair._hold_lease() is True                       # we take it
    assert repair._hold_lease() is True                       # ours stays ours
    db.kv_set("repair:lease", {"pid": os.getpid() + 99999, "ts": _t.time()})
    assert repair._hold_lease() is False, "a live foreign holder must block us"
    db.kv_set("repair:lease", {"pid": os.getpid() + 99999,    # holder died long ago
                               "ts": _t.time() - repair.TICK_SECONDS * 4})
    assert repair._hold_lease() is True, "a stale lease must expire"


def test_a_stale_state_pointing_at_a_missing_sprint_resets(fresh_db, no_spend):
    repair.toggle(True)
    repair.set_state(phase="build", sprint_no=99, task_idx=0)
    asyncio.run(repair.tick())
    assert repair.state()["phase"] in ("idle", "scout", "sleeping")


def test_restarting_phase_resumes_to_idle(fresh_db, no_spend, monkeypatch):
    monkeypatch.setattr(config, "AUTH_CONFIGURED", False)
    repair.toggle(True)
    repair.set_state(phase="restarting", sprint_no=1)
    asyncio.run(repair.tick())
    assert repair.state()["phase"] != "restarting"


# --------------------------------------------------------------------------
# meters + sleep
# --------------------------------------------------------------------------

def test_meters_window_math_is_exact(fresh_db):
    now = 1_000_000_000.0
    rows = ([{"ts": now - 4 * 3600, "kind": "build", "model": "m", "usd": 0}] * 3
            + [{"ts": now - 6 * 3600, "kind": "build", "model": "m", "usd": 0}] * 2
            + [{"ts": now - 8 * 86400, "kind": "scout", "model": "m", "usd": 0}])
    db.kv_set("repair:ledger", rows)
    m = repair.meters(now)
    assert m["s5h"]["used"] == 3                     # only the 4h-old ones
    assert m["w7d"]["used"] == 5                     # the 8-day-old one rolled off
    assert m["s5h"]["wake"] == 0                     # under cap → no wake time


def test_the_manager_sleeps_on_a_model_cooldown(fresh_db):
    model = str(tuning.get("repair_builder_model"))
    db.set_cooldown(model, time.time() + 600, "session limit")
    launcher.load_cooldowns()
    ok, reason, wake = repair.headroom()
    assert not ok and "cooling" in reason and wake > time.time()
    db.set_cooldown(model, time.time() - 1, "over")
    launcher.load_cooldowns()


def test_the_real_session_limit_string_puts_the_engine_to_sleep(fresh_db):
    repair.toggle(True)
    assert repair._rate_limited("You've hit your session limit · resets 3pm") is True
    st = repair.state()
    assert st["phase"] == "sleeping" and st["sleep_until"] > time.time()
    model = str(tuning.get("repair_builder_model"))
    assert launcher.cooldown_left(model) > 0
    db.set_cooldown(model, time.time() - 1, "over")
    launcher.load_cooldowns()


def test_a_headroom_sleep_wakes_early_but_a_pause_waits_it_out(fresh_db, no_spend, monkeypatch):
    """Sprint 3's lesson: the wake time is a GUESS. When the window rolls sooner than guessed,
    sitting out a stale clock (while the meter reads 3/6) is both wrong and confusing — so a
    headroom sleep re-checks. The deliberate between-sprints pause is not second-guessed."""
    monkeypatch.setattr(config, "AUTH_CONFIGURED", False)
    repair.toggle(True)
    repair._sleep("session window nearly spent", time.time() + 9999, kind="headroom")
    asyncio.run(repair.tick())                       # headroom is fine now → wakes despite the clock
    assert repair.state()["phase"] != "sleeping"
    repair._sleep("resting between sprints", time.time() + 9999, kind="pause")
    asyncio.run(repair.tick())
    assert repair.state()["phase"] == "sleeping", "a deliberate pause must wait out its clock"


def test_a_recovered_phase_clears_the_stale_error_banner(fresh_db):
    db.kv_set("repair:last_error", {"ts": 1, "phase": "scout", "detail": "old news"})
    repair._clear_error()
    assert not db.kv_get("repair:last_error")


def test_headroom_min_blocks_a_sprint_start(fresh_db):
    now = time.time()
    cap = int(tuning.get("repair_session_cap"))
    db.kv_set("repair:ledger", [{"ts": now - 60, "kind": "build", "model": "m", "usd": 0}] * cap)
    ok, reason, wake = repair.headroom(now)
    assert not ok and wake > now


# --------------------------------------------------------------------------
# factors
# --------------------------------------------------------------------------

def test_factors_seed_toggle_add_remove(fresh_db):
    fs = repair.factors()
    assert len(fs) == 6 and all(f["enabled"] for f in fs)
    repair.set_factors(patch=[{"id": "speed", "enabled": False}])
    assert not next(f for f in repair.factors() if f["id"] == "speed")["enabled"]
    repair.set_factors(add={"name": "Accessibility", "brief": "keyboard + contrast"})
    assert any(f["id"] == "accessibility" for f in repair.factors())
    repair.set_factors(remove="accessibility")
    assert not any(f["id"] == "accessibility" for f in repair.factors())


def test_every_repair_knob_has_a_rationale():
    for knob in ("repair_tasks_per_sprint", "repair_plan_rounds", "repair_supervised",
                 "repair_auto_restart", "repair_session_cap", "repair_weekly_cap",
                 "repair_headroom_min", "repair_builder_model", "repair_max_turns",
                 "repair_scout_max_turns", "repair_fix_attempts", "repair_verify_timeout_s",
                 "repair_sprint_pause_s", "agent_session_cap", "agent_session_window_s"):
        assert knob in tuning.KNOBS and tuning.KNOBS[knob][3], f"{knob} missing or no rationale"


# --------------------------------------------------------------------------
# the builder's git surgery (a disposable repo, never this one)
# --------------------------------------------------------------------------

@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    from app import selfops
    repo = tmp_path / "repo"
    repo.mkdir()
    def g(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    g("init", "-b", "main")
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (repo / "app.py").write_text("x = 1\n")
    (repo / ".gitignore").write_text(".repair/\n.venv/\n")
    (repo / ".venv").mkdir()
    g("add", "-A"); g("commit", "-m", "init")
    monkeypatch.setattr(selfops, "LIVE_TREE", repo)
    return repo


def test_worktree_build_land_revert_cycle(fresh_db, tmp_repo):
    wt = rb.worktree_add("repair/s1-demo", "s1-demo")
    assert wt.exists() and (wt / ".venv").is_symlink()
    (wt / "app.py").write_text("x = 2\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "change"], cwd=wt, check=True, capture_output=True)
    assert rb.protected_violation(wt) is None
    out = rb.land("repair/s1-demo", "demo change")
    assert out["ok"] and out["files"] == ["app.py"]
    assert (tmp_repo / "app.py").read_text() == "x = 2\n"
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_repo, capture_output=True, text=True).stdout
    assert len(log.splitlines()) == 2                      # init + ONE squash commit
    rev = rb.revert(out["sha"])
    assert rev["ok"] and (tmp_repo / "app.py").read_text() == "x = 1\n"
    assert subprocess.run(["git", "status", "--porcelain"], cwd=tmp_repo,
                          capture_output=True, text=True).stdout.strip() == ""


def test_protected_paths_fail_the_branch(fresh_db, tmp_repo):
    wt = rb.worktree_add("repair/s1-bad", "s1-bad")
    (wt / ".env").write_text("SECRET=1\n")
    subprocess.run(["git", "add", "-A", "-f"], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "bad"], cwd=wt, check=True, capture_output=True)
    assert rb.protected_violation(wt) == ".env"
    rb.discard("repair/s1-bad", wt)
    assert not wt.exists()


def test_land_refuses_a_dirty_live_tree(fresh_db, tmp_repo):
    wt = rb.worktree_add("repair/s1-x", "s1-x")
    (wt / "app.py").write_text("x = 3\n")
    subprocess.run(["git", "commit", "-am", "c"], cwd=wt, check=True, capture_output=True)
    (tmp_repo / "app.py").write_text("owner is editing\n")   # dirty the live tree
    out = rb.land("repair/s1-x", "x")
    assert not out["ok"] and "uncommitted" in out["reason"]


# --------------------------------------------------------------------------
# routes: root-gated, and the pinned UI contract
# --------------------------------------------------------------------------

def test_every_repair_endpoint_is_root_gated(client):
    from conftest import _signup
    _signup(client, "muggle")
    client.post("/api/login", json={"username": "muggle", "password": "hunter2pw"})
    assert client.get("/api/repair/status").status_code == 403
    assert client.post("/api/repair/toggle", json={"on": True}).status_code == 403
    assert client.post("/api/repair/factors", json={}).status_code == 403
    assert client.post("/api/repair/abort").status_code == 403


def test_root_flips_the_button_over_http(client):
    from conftest import login
    login(client, "root", "testpass")
    r = client.post("/api/repair/toggle", json={"on": True}).json()
    assert r["enabled"] is True
    s = client.get("/api/repair/status").json()
    assert s["enabled"] is True and "meters" in s and len(s["factors"]) == 6
    client.post("/api/repair/toggle", json={"on": False})


def test_the_new_screen_owns_renderself_and_keeps_the_pins():
    js = dashboard_js()
    assert js.count("async function renderSelf") == 1, "renderSelf must exist exactly once (in repair.js)"
    for pin in ('id="roughIssue"', 'id="refineBtn"', "/api/self/refine",
                'name="sprints"', 'sprints: Number(f.get("sprints"))', "repairToggle" if False else 'name="rpOn"'):
        assert pin in js, f"missing pinned marker {pin}"
    html = (Path(__file__).resolve().parent.parent / "dashboard" / "index.html").read_text()
    assert '<script src="js/repair.js"></script>' in html
