"""Self-repair v2 — the IT crew. Ironclad offline: the button, the state machine, the
meters/sleep math, the builder's git surgery, and the pinned UI contract — all with zero
model calls (a monkeypatched provider raises if anything tries to spend)."""

import asyncio
import subprocess
import sys
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

def test_a_fresh_backlog_skips_the_scout_and_plan_entirely(fresh_db, no_spend, monkeypatch):
    """The economics fix: planning costs ~8 model calls, so it must be amortised. With a fresh
    backlog a sprint goes STRAIGHT to build — no scout, no deliberation, no extraction."""
    monkeypatch.setattr(config, "AUTH_CONFIGURED", False)
    tuning.set("repair_tasks_per_sprint", 1)                      # one per sprint, so the split is visible
    repair.save_backlog({"ts": time.time(), "digest": "d", "tasks": [
        {"slug": "a", "title": "A", "factor": "speed", "brief": "", "acceptance": [],
         "branch": "", "status": "pending", "worktree": None, "verification": None,
         "landed_sha": None, "attempts": 0, "evidence": ""},
        {"slug": "b", "title": "B", "factor": "speed", "brief": "", "acceptance": [],
         "branch": "", "status": "pending", "worktree": None, "verification": None,
         "landed_sha": None, "attempts": 0, "evidence": ""}]})
    repair.toggle(True)
    asyncio.run(repair.tick())                                   # idle → straight to build
    st = repair.state()
    assert st["phase"] == "build", f"a fresh backlog must skip scouting, got {st['phase']}"
    rec = repair.sprint(st["sprint_no"])
    assert [t["title"] for t in rec["tasks"]] == ["A"]            # took its share
    assert [t["title"] for t in repair.backlog()["tasks"]] == ["B"]   # left the rest
    assert rec["scout"]["from_backlog"] is True


def test_a_stale_or_empty_backlog_scouts_again(fresh_db, no_spend, monkeypatch):
    monkeypatch.setattr(config, "AUTH_CONFIGURED", False)
    repair.toggle(True)
    assert repair.backlog_fresh() is False                        # empty
    old = time.time() - (int(tuning.get("repair_backlog_max_age_h")) + 1) * 3600
    repair.save_backlog({"ts": old, "digest": "", "tasks": [{"title": "stale"}]})
    assert repair.backlog_fresh() is False, "an old plan must not drive new sprints"
    asyncio.run(repair.tick())
    assert repair.state()["phase"] == "scout"


def test_the_meter_counts_every_model_call_not_every_row(fresh_db):
    """A deliberation is ONE ledger row but many calls; counting rows made the meter read
    5/6 while ~14 calls had gone out."""
    now = time.time()
    db.kv_set("repair:ledger", [
        {"ts": now - 60, "kind": "plan", "model": "m", "usd": 1.5, "n": 8},
        {"ts": now - 30, "kind": "build", "model": "m", "usd": 0.5, "n": 1},
    ])
    m = repair.meters(now)
    assert m["s5h"]["used"] == 9, "must sum call counts, not rows"
    assert m["s5h"]["usd"] == 2.0
    old = db.kv_get("repair:ledger")
    old.append({"ts": now - 10, "kind": "scout", "model": "m", "usd": 0})   # legacy row, no n
    db.kv_set("repair:ledger", old)
    assert repair.meters(now)["s5h"]["used"] == 10, "rows without n count as one"


def test_a_turn_death_is_not_retried(fresh_db, tmp_repo, monkeypatch):
    """Sprint 3 burned 128 minutes re-running a session that died on turns. A turn-death means
    the task is too big; only a TEST failure is evidence worth retrying on."""
    async def out_of_turns(*a, **k):
        raise RuntimeError("Claude Code returned an error result: Reached maximum number of turns (50)")
    monkeypatch.setattr(rb, "_run_sdk", out_of_turns)
    monkeypatch.setattr(config, "AUTH_CONFIGURED", True)
    monkeypatch.setattr(repair, "_root_settings", lambda: {})
    rec = {"no": 1, "started_at": time.time(), "scout": {}, "memo": None, "landed": 0, "failed": 0,
           "landed_files": [], "retro": "", "tasks": [
               {"slug": "huge", "title": "Huge", "factor": "x", "brief": "", "acceptance": [],
                "branch": "", "status": "pending", "worktree": None, "verification": None,
                "landed_sha": None, "attempts": 0, "evidence": ""}]}
    repair.save_sprint(rec)
    repair.set_state(phase="build", sprint_no=1, task_idx=0)
    asyncio.run(repair.advance(repair.state()))
    t = repair.sprint(1)["tasks"][0]
    assert t["status"] == "failed" and t.get("too_big") is True
    assert "re-scoping" in t["error"]
    assert repair.state()["task_idx"] == 1, "must move on, not retry"


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


def test_a_mid_sprint_sleep_resumes_its_phase_instead_of_restarting_the_sprint(fresh_db, no_spend):
    """Sprint 3's worst bug: sleeping before a build and waking at 'idle' started a NEW sprint
    over the planned one — re-scouting and throwing away work already paid for."""
    repair.toggle(True)
    repair.set_state(sprint_no=7, task_idx=1)
    repair._sleep("session window nearly spent", time.time() + 9999, resume="build")
    assert repair.state()["resume_phase"] == "build"
    seen = {}
    async def capture(st):                            # stop at the wake — advance() is separate
        seen.update(st)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(repair, "advance", capture)
    try:
        asyncio.run(repair.tick())                    # headroom fine → wakes
    finally:
        monkeypatch.undo()
    assert seen["phase"] == "build", "must resume the phase it slept in, not restart the sprint"
    assert seen["sprint_no"] == 7 and seen["task_idx"] == 1


def test_a_failed_build_leaves_no_orphan_branch(fresh_db, tmp_repo, monkeypatch):
    """Sprint 3 orphaned repair/s3-unify-modal-ui-on-inlinedrawer: build() created the branch,
    the session then died on max turns, and the engine never learned the name to clean up."""
    async def die(*a, **k):
        raise RuntimeError("Reached maximum number of turns (50)")
    monkeypatch.setattr(rb, "_run_sdk", die)
    task = {"slug": "big-refactor", "title": "Too big", "brief": "", "acceptance": []}
    with pytest.raises(RuntimeError):
        asyncio.run(rb.build(task, 3, {}))
    assert task["branch"] == "repair/s3-big-refactor" and task["worktree"], "task must know what exists on disk"
    rb.discard(task["branch"], task["worktree"])     # …so the engine's cleanup can work
    out = subprocess.run(["git", "branch", "--list", "repair/*"], cwd=tmp_repo,
                         capture_output=True, text=True).stdout.strip()
    assert out == "", f"orphaned branch left behind: {out}"


def test_a_recovered_phase_clears_the_stale_error_banner(fresh_db):
    db.kv_set("repair:last_error", {"ts": 1, "phase": "scout", "detail": "old news"})
    repair._clear_error()
    assert not db.kv_get("repair:last_error")


def test_the_cap_is_a_ceiling_not_a_starting_gun(fresh_db):
    """Live overshoot (16/14): headroom asked 'is there ANY room' before a deliberation that
    costs ~8 calls. It must ask whether the NEXT phase fits."""
    now = time.time()
    cap = int(tuning.get("repair_session_cap"))
    plan_cost = repair.phase_cost("plan")
    assert plan_cost >= len(repair.enabled_factors()), "a plan costs at least one call per agent"
    # room for a build, not for a plan
    db.kv_set("repair:ledger", [{"ts": now - 60, "kind": "build", "model": "m", "usd": 0,
                                 "n": cap - plan_cost + 1}])
    assert repair.headroom(now, need=1)[0] is True
    ok, reason, wake = repair.headroom(now, need=plan_cost)
    assert not ok and wake > now, "a plan that would blow the cap must wait"


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


# --------------------------------------------------------------------------
# the crew must be six DIFFERENT people
# --------------------------------------------------------------------------

def test_factor_dials_use_vocabulary_the_engine_actually_understands(fresh_db):
    """The first version of these personas used Big Five names (openness, agreeableness).
    `materialise_manifest` drops unknown keys silently, so every specialist was born with a
    neutral psyche and the same dominant drive — six seats saying one sentence six times.
    A dial that lands nowhere is worse than no dial: it reads as configured."""
    from app.lifeworld.drives import SPEC as DRIVES
    from app.lifeworld.psyche import TRAITS
    for f in repair.DEFAULT_FACTORS:
        for k in (f.get("dials") or {}):
            assert k in TRAITS, f"{f['id']} dials an unknown trait {k!r}"
        for k in (f.get("drives") or {}):
            assert k in DRIVES, f"{f['id']} seeds an unknown drive {k!r}"


def test_each_specialist_wants_something_different(fresh_db):
    """A drive is a homeostatic LEVEL: a high number means SATISFIED, so seeding 0.9 for
    'cares deeply about safety' produced an agent that wanted nothing. Six distinct goals is
    the point of a panel — without it the graph is one opinion with five echoes."""
    from app.lifeworld.world import World
    w = World(name="crew")
    goals = []
    for f in repair.DEFAULT_FACTORS:
        h = w.spawn_human(f["name"], dials=dict(f["dials"]))
        for k, v in (f.get("drives") or {}).items():
            h.drives.level[k] = float(v)
        goals.append(h.drives.dominant_goal()[0])
    assert len(set(goals)) == len(goals), f"specialists share goals: {goals}"


def test_the_free_round_gives_every_specialist_its_own_line(fresh_db):
    """The offline stance line keyed only on the dominant drive, so identical psyches
    produced identical text — the literal 'all six saying the same thing' report."""
    from app.lifeworld.world import World
    w = World(name="crew")
    s = w.new_room("table", "freeplay")
    for f in repair.DEFAULT_FACTORS:
        h = w.spawn_human(f["name"], dials=dict(f["dials"]))
        for k, v in (f.get("drives") or {}).items():
            h.drives.level[k] = float(v)
        s.seat(h)
    lines = {s._free_line(h, "You are the crew. Decide what to do next.") for h in s.players()}
    assert len(lines) == len(s.players()), f"repeated stance lines: {lines}"


def test_a_thread_topic_is_what_the_bubble_says_not_the_rulebook(fresh_db):
    """Bubbles read 'On You are the platform's own IT crew planning the NEXT FEW SPRIN…'
    because the topic handed to the free line was the instruction block."""
    from app.lifeworld.world import World
    w = World(name="crew")
    s = w.new_room("table", "freeplay")
    h = w.spawn_human("Correctness", dials={"conscientiousness": 95})
    s.seat(h)
    rulebook = "You are the platform's own IT crew planning the NEXT FEW SPRINTS. Do X. Do Y."
    # With nothing better to go on, the first SENTENCE stands in — never a 70-character slice
    # that stops mid-word, and never the rest of the instruction block.
    fallback = s._free_line(h, rulebook)
    assert "Do X" not in fallback and "SPRINTS —" in fallback
    # But a graph that names its subject gets its subject.
    line = s._free_line(h, rulebook, "sprint 3: what the platform needs next")
    assert line.startswith("sprint 3: what the platform needs next —")
    from app.lifeworld.scene import _topic_of
    assert _topic_of({"topic": "sprint 3: what the platform needs next"}).startswith("sprint 3")
    assert _topic_of({"rulebook": rulebook}) == "", "a rulebook is not a topic"


def test_changing_the_personas_reseats_a_crew_that_already_exists(fresh_db):
    """A persona fix that only reaches fresh installs is not a fix: the running crew keeps
    the psyches it was born with forever."""
    _root_user()
    info = repair.ensure_team()
    assert info and info["personas"] == repair.TEAM_PERSONAS
    stale = dict(info, personas=repair.TEAM_PERSONAS - 1)
    db.kv_set("repair:world", stale)
    again = repair.ensure_team()
    assert again["personas"] == repair.TEAM_PERSONAS, "a stale persona stamp must force a rebuild"


# --------------------------------------------------------------------------
# sleeping on real utilization, not a call counter
# --------------------------------------------------------------------------

def test_the_crew_yields_while_you_are_using_the_quota(fresh_db, no_spend):
    from app import usage
    now = time.time()
    ok, _, _ = repair.headroom(now)
    assert ok, "an idle box must let the crew work"
    usage.note("manager", "claude-opus-5", tok=4000, ts=now - 30)
    ok, why, wake = repair.headroom(now)
    assert not ok and "your own work" in why
    assert wake > now, "a yield must name a time to check back"


def test_the_crew_does_not_yield_to_its_own_spending(fresh_db, no_spend):
    """The crew's deliberation runs through the same provider path a Studio seat does. Filed
    as the owner's, its own spend would read as 'someone else is using the quota' — and it
    would put itself to sleep forever, one tick after starting."""
    from app import usage
    now = time.time()
    usage.note("repair", "claude-sonnet-5", tok=20000, ts=now - 10)
    ok, why, _ = repair.headroom(now)
    assert ok, f"the crew slept because of its own spend: {why}"


def test_attribution_follows_the_caller_not_the_call_site(fresh_db):
    from app import usage
    assert usage.current_source("studio") == "studio"
    with usage.attributed("repair"):
        assert usage.current_source("studio") == "repair"
    assert usage.current_source("studio") == "studio"


def test_your_spending_shrinks_the_crews_allowance(fresh_db):
    from app import usage
    now = time.time()
    budget = int(tuning.get("usage_budget_tokens"))
    share = float(tuning.get("repair_idle_share"))
    assert usage.snapshot(now)["allowance_tok"] == int(budget * share)
    usage.note("worker", "claude-sonnet-5", tok=int(budget * share), ts=now - 60)
    u = usage.snapshot(now)
    assert u["allowance_tok"] == 0, "the owner's work must take the room back"
    assert u["idle_frac"] < 1.0


def test_a_quiet_hour_stops_counting_as_contention(fresh_db, no_spend):
    from app import usage
    now = time.time()
    quiet = int(tuning.get("repair_yield_quiet_s"))
    usage.note("manager", "claude-opus-5", tok=500, ts=now - quiet - 60)
    ok, why, _ = repair.headroom(now)
    assert ok, f"the crew must claim genuinely idle quota: {why}"
    assert usage.snapshot(now)["contended"] is False


def test_a_real_rate_limit_still_outranks_every_utilization_number(fresh_db, no_spend):
    """The provider's own words are the only authoritative signal; a meter that says
    'plenty left' cannot talk the crew into a wall."""
    model = str(tuning.get("repair_builder_model"))
    launcher.note_rate_limit(model, "You've hit your session limit · resets 3pm")
    ok, why, _ = repair.headroom(time.time())
    assert not ok and "cooling" in why


# --------------------------------------------------------------------------
# transparency: the board and the activity stream
# --------------------------------------------------------------------------

def test_the_board_endpoint_returns_a_whole_sprint(client):
    from conftest import login
    login(client, "root", "testpass")
    db.kv_set("repair:sprint:4", {"no": 4, "retro": "one landed", "landed": 1, "failed": 1,
                                  "tasks": [{"title": "fix a race", "status": "landed", "landed_sha": "abc1234"},
                                            {"title": "tidy the css", "status": "failed", "error": "suite red"}]})
    r = client.get("/api/repair/sprint/4")
    assert r.status_code == 200
    s = r.json()["sprint"]
    assert [t["status"] for t in s["tasks"]] == ["landed", "failed"]
    assert client.get("/api/repair/sprint/99").status_code == 404


def test_the_activity_endpoint_shows_the_crew_and_only_the_crew(client):
    from conftest import login
    from app import bus
    login(client, "root", "testpass")
    bus.emit(0, None, "repair", "repair_landed", {"task": "fix a race", "sha": "abc1234"})
    bus.emit(0, None, "system", "something_else", {})
    kinds = [e["kind"] for e in client.get("/api/repair/activity").json()["events"]]
    assert "repair_landed" in kinds and "something_else" not in kinds


def test_the_board_and_activity_are_root_gated_like_everything_else(client):
    from conftest import _signup
    _signup(client, "muggle2")
    client.post("/api/login", json={"username": "muggle2", "password": "hunter2pw"})
    assert client.get("/api/repair/sprint/1").status_code == 403
    assert client.get("/api/repair/activity").status_code == 403


def test_the_screen_shows_utilization_the_board_and_the_activity_feed():
    js = dashboard_js()
    for pin in ("rpUtilHtml", "rpBoard", "rpActivity", "rpOnEvent",
                "/api/repair/activity", "/api/repair/sprint/"):
        assert pin in js, f"missing {pin}"
    # the shared socket has to actually feed it, or the feed is a 5s poll pretending to be live
    assert "rpOnEvent(e)" in js


def test_a_headroom_sleep_does_not_repeat_itself_into_the_feed(fresh_db, no_spend):
    """A headroom sleep is re-decided every tick. Emitting each time produced one identical
    'sleeping' line every 20 seconds — 180 an hour, burying everything the crew actually did."""
    from app import bus
    n = lambda: len([e for e in db.list_events(0, limit=500) if e["kind"] == "repair_sleeping"])
    before = n()
    for _ in range(5):
        repair._sleep("session window nearly spent", time.time() + 60)
    assert n() == before + 1, "only the change is news"
    repair._sleep("claude-sonnet-5 is cooling down (limit hit)", time.time() + 600, kind="cooldown")
    assert n() == before + 2, "a different reason IS news"
    assert bus is not None


def test_the_call_counter_is_a_fallback_not_a_veto(fresh_db, no_spend):
    """An idle subscription and a crew asleep on '14 calls in 5 hours' was the whole
    complaint. Where real spend is measured, the measurement decides; the counter only
    speaks for boxes whose SDK reports no cost at all."""
    from app import usage
    now = time.time()
    cap = int(tuning.get("repair_session_cap"))
    db.kv_set("repair:ledger", [{"ts": now - 60, "kind": "build", "model": "m", "usd": 0, "n": cap}])
    ok, why, _ = repair.headroom(now)
    assert not ok and "session window" in why, "with no cost signal the counter still rules"
    # now the same box, but the SDK is reporting cost and the window is plainly idle
    usage.note("repair", "claude-sonnet-5", tok=2000, ts=now - 60)
    ok, why, _ = repair.headroom(now)
    assert ok, f"a measured-idle window must beat the proxy: {why}"
    # ...and a measured-FULL window still stops it
    usage.note("repair", "claude-sonnet-5",
               tok=int(int(tuning.get("usage_budget_tokens")) * float(tuning.get("repair_idle_share"))),
               ts=now - 60)
    ok, why, _ = repair.headroom(now)
    assert not ok and "share of this window" in why


def test_the_crews_own_history_reaches_the_shared_meter_once(fresh_db):
    """Until the crew's existing ledger shows up in the shared meter, utilization reads
    '$0 used' on a box that has spent real money all week — and the crude counter keeps
    deciding. It must also run exactly once: twice would double the crew's apparent spend."""
    from app import usage
    now = time.time()
    db.kv_set("repair:ledger", [{"ts": now - 60, "kind": "build", "model": "m", "usd": 1.25, "n": 1},
                                {"ts": now - 30, "kind": "plan", "model": "m", "usd": 0, "n": 8}])
    assert usage.backfill_repair() == 1, "only rows with a cost are worth importing"
    assert usage.snapshot(now)["calls"] == 1, "imported as calls — those rows carry no tokens"
    assert usage.snapshot(now)["repair_tok"] == 0, "an honest zero beats an invented number"
    assert usage.backfill_repair() == 0
    assert usage.snapshot(now)["calls"] == 1, "must not double-count"


def test_a_sprints_provider_calls_are_billed_to_the_crew(fresh_db, no_spend, monkeypatch):
    """`advance` wraps every phase in the crew's attribution, so the deliberation's calls —
    which go through the very same provider path a Studio seat uses — are not mistaken for
    the owner's work by the contention check."""
    from app import usage
    seen = {}

    async def fake_advance(st):
        seen["source"] = usage.current_source("studio")
    monkeypatch.setattr(repair, "_advance", fake_advance)
    asyncio.run(repair.advance({"phase": "idle"}))
    assert seen["source"] == "repair"


def test_the_activity_feed_collapses_repeats_and_names_the_empty_states():
    js = dashboard_js()
    assert "function rpCollapse" in js, "repeated beats must collapse, not scroll the news away"
    assert "rpActText" in js and "rpActLine(g.e, g.n)" in js
    # an enabled engine mid-scout must not tell the reader to flip a switch that is already on
    assert "no sprint yet — flip the switch" in js and "scouting the repo — nothing planned yet" in js
    assert "This sprint has not taken on any work yet." in js


def test_a_persona_revision_reaches_a_crew_that_already_exists(fresh_db):
    """The factor list is kv-persisted, so DEFAULT_FACTORS only ever seeded a fresh install:
    the live crew kept its day-one psyches through every fix. What the OWNER chose (which
    factors are on, which they added) must survive the refresh; what the CODE defines must not."""
    stale = [{"id": "correctness", "name": "Correctness", "enabled": False, "brief": "old",
              "dials": {"openness": 40}},                       # a trait this engine never had
             {"id": "mine", "name": "Mine", "enabled": True, "brief": "my own", "dials": {}}]
    db.kv_set("repair:factors", stale)
    db.kv_set("repair:factors_v", 0)
    out = {f["id"]: f for f in repair.factors()}
    assert out["correctness"]["dials"] == dict(DEFAULTS := repair.DEFAULT_FACTORS[0]["dials"])
    assert out["correctness"]["drives"] == repair.DEFAULT_FACTORS[0]["drives"]
    assert out["correctness"]["enabled"] is False, "the owner's own toggle must survive"
    assert out["mine"]["brief"] == "my own", "a factor the owner added is theirs"
    assert db.kv_get("repair:factors_v") == repair.TEAM_PERSONAS
    assert DEFAULTS  # silence the walrus lint


def test_a_rulebook_cannot_squeeze_the_round_out_of_the_hosts_reply(fresh_db):
    """Every rulebook LINE was handed to the host as a rule to echo. A prose rulebook then
    filled the reply with rule echoes, the JSON truncated, the parse failed — and the round
    silently fell back to canned lines with the reason recorded nowhere."""
    from app.lifeworld import world as wmod
    assert wmod.MAX_RULES <= 12
    src = Path(wmod.__file__).read_text()
    assert "60 * len(rules)" in src, "the token budget must account for the rules it asked for"
    assert "self.host_error =" in src, "a mediated round that degrades must say why"


def test_the_server_can_actually_be_stopped():
    """Background loops are written never to die — correct while the server is up, wrong the
    moment it is going down. Nothing cancelled them, so a SIGTERM'd process could keep its
    event loop alive, still holding the repair lease and still ticking the engine against the
    same database as its replacement. That is the 'why is another devteam running' bug."""
    src = Path(__file__).resolve().parents[1].joinpath("conductor/app/main.py").read_text()
    head, _, tail = src.partition("    yield")
    assert "background.append(loop.create_task(repair.loop()))" in head, \
        "every never-die loop must be tracked"
    starts = [ln.strip() for ln in head.splitlines() if "loop.create_task(" in ln]
    assert starts, "the lifespan should still be starting background work"
    for ln in starts:
        assert ln.startswith("background.append(") or ln.startswith("_manager_tasks["), \
            f"untracked background task cannot be stopped: {ln}"
    assert "t.cancel()" in tail and "asyncio.wait(dying, timeout=" in tail, \
        "shutdown must cancel them and wait, bounded"


def test_a_session_that_writes_outside_its_worktree_fails_the_task(fresh_db, tmp_repo, monkeypatch):
    """Sprint 11's real failure: the build session edited the OPERATOR'S checkout instead of
    its worktree. The operator was committing at the time, so half-finished, never-verified
    work rode into main inside an unrelated commit. The worktree is a convention, not a jail —
    a session with Bash can write anywhere — so the only honest check is to photograph the
    live tree around the session."""
    monkeypatch.setattr(rb.selfops, "LIVE_TREE", tmp_repo)
    task = {"slug": "x", "title": "X", "brief": "", "acceptance": []}

    async def sneaky(system, prompt, cwd, tools, turns, model, env):
        # The signature of a real escape: the same file written in BOTH trees, because the
        # file is what the session was working on.
        for root in (Path(cwd), tmp_repo):
            (root / "conductor").mkdir(exist_ok=True)
            (root / "conductor" / "trespass.py").write_text("# not my tree\n")
        return "done", 0.0
    monkeypatch.setattr(rb, "_run_sdk", sneaky)
    with pytest.raises(RuntimeError) as e:
        asyncio.run(rb.build(task, 1, {}))
    assert "outside its worktree" in str(e.value) and "trespass.py" in str(e.value)
    # and it must NOT clean up after itself: that tree belongs to a person
    assert (tmp_repo / "conductor" / "trespass.py").exists()


def test_the_operators_own_edits_do_not_fail_the_crews_task(fresh_db, tmp_repo, monkeypatch):
    """Sprints 13 and 17 were failed for files the OPERATOR was editing at that instant, and
    the crew's finished work was thrown away for it. A session that truly wrote outside its
    worktree almost always wrote the same file inside it too; anything else is somebody
    else's edit, and is reported rather than punished."""
    monkeypatch.setattr(rb.selfops, "LIVE_TREE", tmp_repo)
    task = {"slug": "y", "title": "Y", "brief": "", "acceptance": []}

    async def works_properly(system, prompt, cwd, tools, turns, model, env):
        (Path(cwd) / "in_the_worktree.py").write_text("# proper work\n")
        (tmp_repo / "the_operator_was_here.py").write_text("# my own edit\n")   # a human, mid-session
        return "done", 0.0
    monkeypatch.setattr(rb, "_run_sdk", works_properly)
    out = asyncio.run(rb.build(task, 1, {}))
    assert out["branch"], "the crew's work must survive somebody else typing"


def test_the_builder_is_told_to_stay_in_its_worktree():
    assert "never use an absolute path" in rb.BUILDER_SYSTEM
    assert "operator's live tree" in rb.BUILDER_SYSTEM


def test_the_process_that_owns_the_port_owns_the_engine(fresh_db, no_spend):
    """The lease stopped two engines racing, but it let the WRONG one win: a server told to
    stop, port already released, kept heartbeating — so the new server stood down and the crew
    silently did nothing while a zombie drove it on stale code. Binding the port is the only
    unforgeable proof of being the current conductor."""
    import os
    db.kv_set("repair:lease", {"pid": os.getpid() + 99999, "ts": time.time()})   # a live zombie
    assert repair._hold_lease() is False
    repair.claim_lease()                                                         # we bound the port
    assert repair._hold_lease() is True
    assert db.kv_get("repair:lease")["pid"] == os.getpid()
    src = Path(__file__).resolve().parents[1].joinpath("conductor/app/main.py").read_text()
    assert "repair.claim_lease()" in src, "startup must claim it, or the fix is theoretical"


# --------------------------------------------------------------------------
# the screen: tabs, and tokens instead of dollars
# --------------------------------------------------------------------------

def test_usage_is_reported_in_tokens_not_dollars(fresh_db):
    """The owner is on a Max subscription: there is no bill, so a dollar figure answers no
    question they have. Tokens are what the limit is made of."""
    from app import usage
    now = time.time()
    usage.note("manager", "claude-opus-5", tok=12_000, cache=900_000, usd=1.5, ts=now - 30)
    u = usage.snapshot(now)
    assert u["owner_tok"] == 12_000
    assert u["cache_tok"] == 900_000, "cache is counted, but apart"
    assert u["used_tok"] == 12_000, "cache must not inflate the number the meter runs on"
    assert u["by_source"]["manager"] == {"tok": 12_000, "cache": 900_000, "calls": 1}


def test_a_result_message_is_split_into_work_and_cache(fresh_db):
    from app import usage

    class R:
        total_cost_usd = 0.5
        usage = {"input_tokens": 900, "output_tokens": 100,
                 "cache_read_input_tokens": 3_000_000, "cache_creation_input_tokens": 20_000}
    usage.note_result("repair", "claude-sonnet-5", R())
    row = usage.rows()[-1]
    assert row["tok"] == 1000 and row["cache"] == 3_020_000


def test_a_worker_reports_its_tokens_home(client, fresh_db):
    """The conductor meters the whole box; a worker that reports only a dollar figure leaves
    the crew guessing at how much of the window a human's task just used."""
    src = Path(__file__).resolve().parents[1].joinpath("worker/worker.py").read_text()
    assert '"tokens": SESSION_TOKENS["tok"]' in src and "def note_tokens" in src
    from app import routes
    assert "tokens" in routes.WorkerReport.model_fields
    assert routes.WorkerReport.model_fields["tokens"].default == 0, "an older worker must still report"


def test_the_screen_is_tabbed_and_every_panel_stays_in_the_dom():
    js = dashboard_js()
    tabs = js.split("const RP_TABS = [", 1)[1].split("];", 1)[0]
    for t in ("crew", "board", "activity", "usage", "command"):
        assert f'id: "{t}"' in tabs, f"no {t} tab"
    assert 'data-tab="${t.id}"' in js and 'data-panel="${t.id}"' in js
    assert "let rpTab" in js, "the open tab must survive the status poll"
    # panels are hidden, not dropped: a half-typed ticket has to survive a tab change
    assert 'p.hidden = p.dataset.panel !== rpTab' in js
    # and the pinned ticket form still exists no matter which tab is open
    for pin in ('id="roughIssue"', 'id="refineBtn"', 'name="sprints"'):
        assert pin in js


def test_the_screen_shows_no_dollar_figures():
    """Root asked for this directly: dollars mean nothing on a subscription."""
    js = dashboard_js()
    panel = js.split("function rpUtilHtml", 1)[1].split("function rpCommandPanel", 1)[0]
    assert "usd" not in panel, "the Improve screen must not talk in dollars"
    assert "rpTok(" in panel and "tokens" in panel


def test_a_token_less_row_still_counts_as_a_call(fresh_db):
    """A session that reported a cost but no tokens — the shape every pre-split
    row has, and every SDK that does not report usage still produces — must count
    as a CALL and as zero tokens. Otherwise the meter either reads 100% on a
    fiction or hides the session from the count-based backstop entirely.

    The pre-split rows THEMSELVES (a `tokens` field summing all four kinds, one
    build's 3M cache reads inside it) arrive through the usage service's
    first-boot expansion of the old kv blob, and that is where they are drilled:
    services/usage/tests/test_usage_smoke.py.
    """
    from app import usage
    now = time.time()
    usage.note("repair", "m", usd=1.7, calls=1, ts=now - 60)
    u = usage.snapshot(now)
    assert u["used_tok"] == 0 and u["frac"] == 0
    assert u["calls"] == 1, "still visible as a call — that is what the backstop counts"
    assert u["usd"] == 1.7


def test_the_self_repair_doc_is_generated_from_the_code_and_current():
    """Hand-written architecture docs rot silently: the sentence stays true-sounding while the
    constant it describes changes underneath. This one is read out of the modules, and this
    test is what makes forgetting to regenerate impossible."""
    import subprocess
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "tools/gen_selfrepair_doc.py", "--check"],
                       cwd=root, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    doc = (root / "docs" / "SELF_REPAIR.md").read_text()
    assert doc.startswith("<!-- GENERATED"), "it must say it is generated, or someone will edit it"
    # the things most likely to drift are all present
    for must in ("## The phases", "## When it sleeps", "## Knobs", "## HTTP",
                 "usage_budget_tokens", "/api/repair/activity", "repair:lease"):
        assert must in doc, f"the doc lost {must}"


def test_the_backstop_is_hidden_when_it_decides_nothing():
    """A red 34/14 sitting beside a plan with plenty left is not information, it is alarm.
    The counters appear only while they are the thing actually deciding."""
    js = dashboard_js()
    assert '${(u.used_tok || 0) > 0 ? "" : `<div class="rp-card">' in js
    assert "deciding, because nothing reported tokens this window" in js


def test_the_usage_screen_says_whose_usage_it_is():
    """Root compared it against a plugin that watches their Claude account and reasonably
    asked what this number even is. It is what THIS platform spent — not the account."""
    js = dashboard_js()
    assert "not your Claude account's usage" in js
    assert "usage_budget_tokens" in js and "no endpoint that reports one" in js


def test_a_sleeping_engine_keeps_its_reason_current(fresh_db, no_spend):
    """The reason was written when it fell asleep and never revisited, so the banner could
    read 'used its share of this window' beside a meter showing the window 100% idle."""
    repair.toggle(True)
    repair._sleep("self-repair has used its share of this window", time.time() + 3600)
    # the real reason now is the session counter, not the share
    cap = int(tuning.get("repair_session_cap"))
    db.kv_set("repair:ledger", [{"ts": time.time(), "kind": "build", "model": "m", "usd": 0, "n": cap}])
    asyncio.run(repair.tick())
    st = repair.state()
    assert st["phase"] == "sleeping"
    assert "session window" in st["sleep_reason"], f"stale reason: {st['sleep_reason']}"


# --------------------------------------------------------------------------
# the log pipeline
# --------------------------------------------------------------------------

def test_a_log_row_is_levelled_categorised_and_machine_readable(fresh_db, monkeypatch):
    from app import logs
    monkeypatch.setattr(logs, "ECHO", False)
    r = logs.warn("quota", "rate_limited", "sonnet is cooling down", model="claude-sonnet-5")
    assert r["level"] == "warn" and r["cat"] == "quota" and r["event"] == "rate_limited"
    assert r["model"] == "claude-sonnet-5", "fields ride along, not glued into the sentence"
    assert logs.rows()[-1]["event"] == "rate_limited"


def test_an_unknown_category_is_corrected_rather_than_invented(fresh_db, monkeypatch):
    """The vocabulary is the axis people filter on. A typo that silently created a new
    category would split the very history someone is searching."""
    from app import logs
    monkeypatch.setattr(logs, "ECHO", False)
    assert logs.info("repair_builder", "x")["cat"] == "lifecycle"
    assert set(logs.CATEGORIES) >= {"sprint", "session", "spend", "sleep", "quota",
                                    "git", "verify", "sandbox", "lifecycle"}


def test_level_filtering_is_a_floor_not_an_equality(fresh_db, monkeypatch):
    """Someone asking for warnings is looking for trouble; hiding errors behind a stricter
    filter is the opposite of what they meant."""
    from app import logs
    monkeypatch.setattr(logs, "ECHO", False)
    logs.debug("git", "a"); logs.info("git", "b"); logs.warn("git", "c"); logs.error("git", "d")
    assert [r["event"] for r in logs.recent(level="warn")] == ["c", "d"]
    assert [r["event"] for r in logs.recent(level="debug")] == ["a", "b", "c", "d"]
    assert [r["event"] for r in logs.recent(cat="git", event="b")] == ["b"]


def test_errors_survive_a_chatty_hour(fresh_db, monkeypatch):
    """The ring is bounded, so a loop that logs every tick would otherwise push the one row
    anyone actually wants out of the record."""
    from app import logs
    monkeypatch.setattr(logs, "ECHO", False)
    monkeypatch.setattr(logs, "MAX_ROWS", 10)
    logs.error("sandbox", "escape", "wrote into the live checkout")
    for i in range(30):
        logs.info("sprint", f"noise_{i}")
    assert not [r for r in logs.rows() if r["event"] == "escape"], "the ring is bounded"
    assert [r["event"] for r in logs.rows(errors_only=True)] == ["escape"]


def test_stats_turn_a_count_into_a_diagnosis(fresh_db, monkeypatch):
    from app import logs
    monkeypatch.setattr(logs, "ECHO", False)
    logs.error("sandbox", "escape"); logs.error("sandbox", "escape"); logs.info("git", "landed")
    st = logs.stats(3600)
    assert st["by_level"]["error"] == 2 and st["by_cat"]["sandbox"] == 2
    assert st["last_error"]["cat"] == "sandbox"
    assert st["categories"]["sandbox"], "the vocabulary travels with the numbers"


def test_the_engine_logs_the_moments_that_matter(fresh_db, no_spend, monkeypatch):
    """A phase machine whose history exists only as the CURRENT value of one kv key cannot be
    diagnosed after the fact — by the time anyone looks, it has moved on."""
    from app import logs
    monkeypatch.setattr(logs, "ECHO", False)
    monkeypatch.setattr(config, "AUTH_CONFIGURED", False)
    _root_user()
    repair.toggle(True)
    for _ in range(4):
        asyncio.run(repair.tick())
    cats = {r["cat"] for r in logs.rows()}
    assert "sprint" in cats, "phase transitions must be on the record"
    assert any(r["event"].startswith("phase_") for r in logs.rows())


def test_the_log_endpoints_are_root_gated(client):
    from conftest import _signup
    _signup(client, "snoop")
    client.post("/api/login", json={"username": "snoop", "password": "hunter2pw"})
    for path in ("/api/logs", "/api/logs/stats", "/api/logs/errors"):
        assert client.get(path).status_code == 403, f"{path} leaks"


def test_root_can_read_and_filter_the_logs(client, fresh_db, monkeypatch):
    from conftest import login
    from app import logs
    monkeypatch.setattr(logs, "ECHO", False)
    login(client, "root", "testpass")
    logs.error("sandbox", "escape", "wrote into the live checkout", files="a.py")
    logs.info("git", "landed", "a fix", sha="abc1234")
    r = client.get("/api/logs?level=warn").json()
    assert [x["event"] for x in r["logs"]] == ["escape"]
    assert "sandbox" in r["categories"] and "error" in r["levels"]
    assert client.get("/api/logs?q=abc1234").json()["logs"][0]["event"] == "landed"
    assert client.get("/api/logs/stats").json()["by_cat"]["sandbox"] == 1


def test_the_activity_tab_can_show_the_pipeline_not_just_the_story():
    js = dashboard_js()
    assert "rpActView" in js and 'data-view="logs"' in js and 'data-view="story"' in js
    assert "/api/logs?" in js, "the log view must actually read the pipeline"
    assert "rpFillCats" in js, "the category vocabulary has one home: the server"
    # a live crew event must not scribble over the log view
    assert 'rpActView !== "story"' in js


def test_every_way_a_task_can_end_leaves_a_log_row(fresh_db, tmp_repo, no_spend, monkeypatch):
    """The three endings that matter — the suite went red, the session died, something wrote
    where it should not — each need a row of their own, or "the sprint failed" is all anyone
    can find out afterwards."""
    from app import logs
    monkeypatch.setattr(logs, "ECHO", False)
    src = Path(__file__).resolve().parents[1].joinpath("conductor/app/repair.py").read_text()
    for event in ("suite_red", "build_died", "protected_path", "task_abandoned", "build_retry"):
        assert f'"{event}"' in src, f"nothing logs {event}"
    # and each is filed under the category someone would actually look in
    assert 'logs.error("sandbox", "protected_path"' in src
    assert 'logs.error("session", "build_died"' in src


def test_a_repeated_warning_is_counted_not_repeated(fresh_db, monkeypatch):
    """A never-die loop that finds the same thing wrong every 20 seconds writes 180 identical
    lines an hour, and the record becomes unreadable exactly when someone needs it."""
    from app import logs
    monkeypatch.setattr(logs, "ECHO", False)
    logs._LAST.clear()
    for _ in range(5):
        logs.log("lifecycle", "lease_held_elsewhere", "standing down", level="warn", dedupe_s=600)
    rows = [r for r in logs.rows() if r["event"] == "lease_held_elsewhere"]
    assert len(rows) == 1, "the repetition must collapse"
    assert rows[0]["repeats"] == 5, "but the count has to survive"
    logs._LAST.clear()                       # a later window is news again
    logs.log("lifecycle", "lease_held_elsewhere", "standing down", level="warn", dedupe_s=600)
    assert len([r for r in logs.rows() if r["event"] == "lease_held_elsewhere"]) == 2


def test_log_timestamps_do_not_wrap():
    """"12:07:37 PM" did not fit the column and broke onto two lines, doubling the height of
    every row in a view whose whole point is scanning a lot of them."""
    js = dashboard_js()
    assert 'hourCycle: "h23"' in js


# --------------------------------------------------------------------------
# the monitor: notices, proposals, approval
# --------------------------------------------------------------------------

def test_a_notice_carries_its_evidence_and_a_proposal(fresh_db, monkeypatch):
    from app import logs, monitor
    monkeypatch.setattr(logs, "ECHO", False)
    logs.error("sandbox", "escape", "wrote into the live checkout", files="conductor/app/deploy.py")
    ns = monitor.scan()
    n = next(x for x in ns if x["kind"] == "sandbox")
    assert n["severity"] == "critical"
    assert "deploy.py" in n["detail"], "a notice has to name the thing it is about"
    assert n["evidence"] and n["evidence"][0]["event"] == "escape"
    assert n["action"] == "pause_repair", "and offer the obvious next move"


def test_a_notice_that_stops_applying_stops_being_reported(fresh_db, monkeypatch):
    """Derived on read, like project blockers: nothing to clean up, and the list can never
    show a problem that has already gone away."""
    from app import logs, monitor
    monkeypatch.setattr(logs, "ECHO", False)
    logs.error("sandbox", "escape", "wrote outside", files="x.py")
    assert any(n["kind"] == "sandbox" for n in monitor.scan())
    db.kv_set(logs.RING_KEY, [])
    assert not any(n["kind"] == "sandbox" for n in monitor.scan())


def test_dismissing_a_notice_silences_that_one_only(fresh_db, monkeypatch):
    from app import logs, monitor
    monkeypatch.setattr(logs, "ECHO", False)
    logs.error("sandbox", "escape", "wrote outside", files="x.py")
    logs.error("http", "boom", "something else broke")
    n = next(x for x in monitor.scan() if x["kind"] == "sandbox")
    monitor.decide(n["fp"], "dismissed")
    kinds = {x["kind"] for x in monitor.scan()}
    assert "sandbox" not in kinds and "errors" in kinds
    assert any(x["kind"] == "sandbox" for x in monitor.scan(include_decided=True))


def test_approving_a_proposal_actually_does_the_thing(fresh_db, no_spend, monkeypatch):
    """The whole point: a proposal you can act on without going to read the code. And the
    blast radius is knowable — ACTIONS is the complete list of what Approve can do."""
    from app import logs, monitor
    monkeypatch.setattr(logs, "ECHO", False)
    repair.toggle(True)
    logs.error("sandbox", "escape", "wrote outside", files="x.py")
    n = next(x for x in monitor.scan() if x["kind"] == "sandbox")
    out = asyncio.run(monitor.approve(n["fp"]))
    assert out["ok"] and repair.enabled() is False, "approval must actually pause it"
    assert monitor.decisions()[n["fp"]]["state"] == "approved"
    assert set(monitor.ACTIONS) == {"pause_repair", "set_knob", "abort_task", "file_task"}, \
        "a new action is a deliberate widening of what Approve can do"


def test_nothing_acts_without_approval(fresh_db, no_spend, monkeypatch):
    from app import logs, monitor
    monkeypatch.setattr(logs, "ECHO", False)
    repair.toggle(True)
    logs.error("sandbox", "escape", "wrote outside", files="x.py")
    monitor.scan(); monitor.scan(); monitor.summary()
    assert repair.enabled() is True, "scanning must never change anything"


def test_a_broken_rule_cannot_blind_the_others(fresh_db, monkeypatch):
    from app import logs, monitor
    monkeypatch.setattr(logs, "ECHO", False)

    def boom(_rows):
        raise ValueError("bad rule")
    monkeypatch.setattr(monitor, "RULES", [boom] + list(monitor.RULES))
    logs.error("http", "boom", "something broke")
    assert any(n["kind"] == "errors" for n in monitor.scan())


def test_the_notice_endpoints_are_root_gated(client):
    from conftest import _signup
    _signup(client, "peeker")
    client.post("/api/login", json={"username": "peeker", "password": "hunter2pw"})
    assert client.get("/api/logs/notices").status_code == 403
    assert client.post("/api/logs/notices/approve", json={"fp": "x"}).status_code == 403
    assert client.post("/api/logs/notices/dismiss", json={"fp": "x"}).status_code == 403


def test_the_screen_shows_notices_shipped_work_and_your_tickets():
    js = dashboard_js()
    assert 'id: "notices"' in js and "rpNoticesPanel" in js and "/api/logs/notices" in js
    assert "data-approve-fp" in js and "data-dismiss-fp" in js
    # self-repair lands commits, not PRs — the screen has to link the thing that exists
    assert 'gitWebUrl(repo, "commit"' in js and 'kind === "commit"' in dashboard_js()
    assert "repairTickets" in js and "data-open-project" in js


def test_the_chat_shows_its_history_without_spending(fresh_db):
    """It started hidden and empty, so it read as a dead input box: you typed, waited, and
    had no way to tell whether anything was happening. An empty POST returns the log."""
    js = dashboard_js()
    assert "rpChatHistory" in js and "rpRenderChat" in js
    from app.lifeworld.scene import Scene
    import inspect
    src = inspect.getsource(Scene.chat)
    assert "if not text:" in src and "return {\"chat\"" in src


def test_the_stale_banner_offers_a_button_not_a_wrong_command():
    """It ended with a command for a port this setup stopped using — the one instruction on
    screen was wrong, which is worse than no instruction."""
    js = dashboard_js()
    assert "--port 8000" not in js, "the wrong command is back"
    assert 'id="staleRestart"' in js and "/api/repair/restart" in js


def test_every_api_failure_lands_in_the_pipeline(client, fresh_db, monkeypatch):
    """A 500 used to exist only as a stack trace in whatever terminal the server was started
    from, and a 4xx did not exist at all. One middleware means no handler has to remember."""
    from app import logs
    monkeypatch.setattr(logs, "ECHO", False)
    logs._LAST.clear()
    db.kv_set(logs.RING_KEY, [])
    client.get("/api/repair/status")                       # 401/403 — nobody is logged in
    events = {(r["cat"], r["event"]) for r in logs.rows()}
    assert ("auth", "refused") in events, f"nothing recorded the refusal: {events}"
    # ...and successes are NOT logged: an access log is a different thing, and twelve real
    # failures buried under ten thousand 200s is how logs become unreadable
    from conftest import login
    login(client, "root", "testpass")
    db.kv_set(logs.RING_KEY, [])
    client.get("/api/repair/status")
    assert not [r for r in logs.rows() if r["cat"] == "http"], "a 200 must be silent"


def test_a_dashboard_javascript_error_becomes_a_log_row(client, fresh_db, monkeypatch):
    from app import logs
    from conftest import login
    monkeypatch.setattr(logs, "ECHO", False)
    login(client, "root", "testpass")
    db.kv_set(logs.RING_KEY, [])
    client.post("/api/client-error", json={"message": "x is not a function",
                                           "stack": "at rpHtml", "url": "#/improve"})
    row = next(r for r in logs.rows() if r["event"] == "dashboard_error")
    assert "not a function" in row["msg"] and row["level"] == "error"


def test_a_zombie_that_has_been_killed_stops_being_reported(fresh_db, monkeypatch):
    """A live zombie heartbeats every tick; a dead one stops. Reporting a problem that has
    already been solved is how a monitor loses the right to be believed."""
    from app import logs, monitor
    monkeypatch.setattr(logs, "ECHO", False)
    old = time.time() - monitor.ZOMBIE_FRESH_S - 60
    logs.log("lifecycle", "lease_held_elsewhere", "standing down", level="warn", holder=999)
    db.kv_set(logs.RING_KEY, [{**logs.rows()[-1], "ts": old}])
    assert not [n for n in monitor.scan() if n["kind"] == "zombie"]
    logs.log("lifecycle", "lease_held_elsewhere", "standing down now", level="warn", holder=999)
    assert [n for n in monitor.scan() if n["kind"] == "zombie"]


# --------------------------------------------------------------------------
# what kind of work it is, and how much it matters
# --------------------------------------------------------------------------

def test_work_is_classified_by_kind_and_priority(fresh_db):
    """"the crew did 7 things" tells you nothing; "4 bugs, one at P1, 2 features and an idea"
    is a status you can act on."""
    assert set(repair.TASK_TYPES) == {"bug", "feature", "refactor", "chore", "idea"}
    assert set(repair.PRIORITIES) == {"p1", "p2", "p3"}
    assert repair.classify("Port allocation has a check-then-bind race")[0] == "bug"
    assert repair.classify("Migration list swallows real failures")[0] == "bug"
    assert repair.classify("Add a token meter to the usage screen")[0] == "feature"
    assert repair.classify("github.com links hardcoded — extract one helper")[0] == "refactor"
    assert repair.classify("Delete six stray dumps from the repo root")[0] == "chore"
    # p1 is reserved for the genuinely urgent, or the word stops meaning anything
    assert repair.classify("deploy_local runs unsandboxed code")[1] == "p1"
    assert repair.classify("Add a token meter")[1] != "p1"


def test_the_models_own_labels_win_over_the_guess(fresh_db):
    """The heuristic exists for offline sprints and older rows. When the planner says what a
    task is, that is better information than keyword matching and must not be overwritten."""
    t = repair._typed({"title": "Delete stray files", "type": "feature", "priority": "p1"})
    assert (t["type"], t["priority"]) == ("feature", "p1")
    t2 = repair._typed({"title": "Delete stray files", "type": "nonsense", "priority": "p9"})
    assert t2["type"] in repair.TASK_TYPES and t2["priority"] in repair.PRIORITIES


def test_rows_from_before_the_taxonomy_are_labelled_on_read(fresh_db):
    """Existing sprints and backlogs have no type. Showing them as one grey blob would make
    the whole categorisation useless on the only data anyone actually has."""
    db.kv_set("repair:sprint:1", {"no": 1, "tasks": [{"title": "Fix a race", "status": "landed",
                                                      "landed_sha": "abc1234"}]})
    db.kv_set("repair:seq", 1)
    s = repair.status()
    assert s["sprint"]["tasks"][0]["type"] == "bug"
    assert s["landed"][0]["priority"] in repair.PRIORITIES


def test_the_screen_shows_what_kind_of_work_each_thing_is():
    js = dashboard_js()
    assert "function rpTag" in js and "function rpMix" in js
    assert "RP_TYPE" in js and "rp-tag t-${" in js and "rp-tag p-${" in js
    for kind in ("bug", "feature", "refactor", "chore", "idea"):
        assert f"{kind}:" in js.split("const RP_TYPE = {", 1)[1].split("}", 1)[0]
    # and the stylesheet has to actually colour them, or the tag is decoration
    css = (Path(__file__).resolve().parents[1] / "dashboard" / "style.css").read_text()
    for cls in (".rp-tag.t-bug", ".rp-tag.t-feature", ".rp-tag.p-p1"):
        assert cls in css, f"{cls} has no style"


# --------------------------------------------------------------------------
# the review queue tells the truth before you press
# --------------------------------------------------------------------------

def test_the_queue_says_whether_approve_can_work(fresh_db, tmp_repo, monkeypatch):
    """Root pressed Approve because a notice said to, and got a toast that vanished:
    "live tree has uncommitted changes". Pressing it was the only way to find out."""
    monkeypatch.setattr(rb.selfops, "LIVE_TREE", tmp_repo)
    (tmp_repo / "dirty.txt").write_text("mine\n")
    st = rb.landable("main")
    assert st["ok"] is False and "uncommitted" in st["why"]
    assert "commit or stash" in st["fix"], "a refusal has to say what would clear it"


def test_a_stale_branch_can_be_rebuilt_instead_of_binned(fresh_db, tmp_repo, monkeypatch):
    """"main moved since this branch was cut" was a dead end: the work was finished and green,
    and the only offer on screen was to discard it."""
    monkeypatch.setattr(rb.selfops, "LIVE_TREE", tmp_repo)
    src = Path(rb.__file__).read_text()
    assert "def rebuild(" in src and 'rebase' in src
    assert '"remedy": "rebuild"' in src, "the refusal must name its own remedy"
    js = dashboard_js()
    assert "data-rebuild" in js and "/api/repair/queue/rebuild" in js
    assert 'st.ok ? "" : " disabled"' in js, "Approve must not be offered when it cannot work"


def test_the_story_feed_uses_the_same_visual_language_as_a_project():
    """It was flat grey lines; a project's feed has a coloured edge for who spoke and weighs
    outcomes differently from narration. Reusing .ev means both screens read the same way."""
    js = dashboard_js()
    assert "RP_EV_CLASS" in js and "RP_EV_WHO" in js
    assert '`<div class="ev ${cls}">' in js and '<div class="src">' in js
    assert "outcome good" in js and "outcome bad" in js


def test_the_story_feed_does_not_render_its_own_indentation():
    """.ev is `white-space: pre-wrap`, so a prettily-indented template renders the template's
    own newlines and every row comes out three times taller than its content."""
    js = dashboard_js()
    body = js.split("function rpActLine(", 1)[1].split("\n}", 1)[0]
    ret = body.split("return `", 1)[1].split("`;", 1)[0]
    assert "\n" not in ret, f"the .ev template spans lines: {ret[:80]}"


def test_every_function_the_screen_calls_actually_exists():
    """Twice now a block rewrite deleted a helper that lived between two others, and the only
    symptom was "rpLogLine is not defined" in the browser — invisible to every test we had,
    because a missing function is not a syntax error. This is the cheap static check that
    would have caught both."""
    import re
    js = dashboard_js()
    defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", js))
    defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", js))
    called = set(re.findall(r"\b(rp[A-Z]\w*)\s*\(", js))
    missing = sorted(c for c in called if c not in defined)
    assert not missing, f"the screen calls functions that do not exist: {missing}"


def test_an_error_the_ui_catches_is_still_reported(fresh_db):
    """window.onerror only sees what nobody caught, so every `catch (e) { show(e.message) }`
    was invisible to the pipeline. That is exactly how "rpLogLine is not defined" sat on
    screen without ever becoming a log row, a notice, or a bug."""
    js = dashboard_js()
    assert "function reportCaught" in js
    assert "reportClientError(" in js.split("function reportCaught", 1)[1][:400]
    # and the screens that render an error to the user go through it
    rp = js.split("// repair.js", 1)[1]
    for shown in rp.split("catch (e)")[1:]:
        head = shown[:160]
        if "innerHTML" in head and "escapeHtml(" in head:
            assert "reportCaught" in head, f"a caught error is being hidden: {head[:90]}"


def test_a_reported_dashboard_error_becomes_a_queued_bug(fresh_db, no_spend, monkeypatch):
    """The loop the owner asked for: a broken screen reports itself, the monitor turns it
    into a notice, and one click puts it in front of the crew as a typed bug."""
    from app import logs, monitor
    monkeypatch.setattr(logs, "ECHO", False)
    logs.error("http", "dashboard_error", "rpLogLine is not defined", page="#/improve")
    n = next(x for x in monitor.scan() if x["kind"] == "dashboard")
    assert n["action"] == "file_task" and "next sprint" in n["proposal"]
    out = asyncio.run(monitor.approve(n["fp"]))
    assert out["ok"]
    task = repair.backlog()["tasks"][0]
    assert task["type"] == "bug" and task["priority"] == "p2"
    assert "rpLogLine" in task["title"] and task["from_monitor"] is True
    assert "add the test that would have caught it" in task["brief"]


def test_a_queued_bug_goes_to_the_front_of_the_backlog(fresh_db, no_spend, monkeypatch):
    """Burying a live defect under six planned improvements answers "queue it as a bug"
    with "eventually"."""
    from app import logs, monitor
    monkeypatch.setattr(logs, "ECHO", False)
    repair.save_backlog({"ts": time.time(), "digest": "", "tasks": [
        {"slug": "planned", "title": "A planned improvement", "type": "chore", "priority": "p3"}]})
    logs.error("http", "request_crashed", "GET /api/x: boom", path="/api/x")
    n = next(x for x in monitor.scan() if x["kind"] == "dashboard")
    asyncio.run(monitor.approve(n["fp"]))
    titles = [t["title"] for t in repair.backlog()["tasks"]]
    assert titles[0].startswith("Fix:") and "A planned improvement" in titles


def test_filing_the_same_defect_twice_does_not_duplicate_it(fresh_db, no_spend, monkeypatch):
    from app import logs, monitor
    monkeypatch.setattr(logs, "ECHO", False)
    logs.error("http", "dashboard_error", "x is not a function", page="#/improve")
    n = next(x for x in monitor.scan() if x["kind"] == "dashboard")
    asyncio.run(monitor.approve(n["fp"]))
    asyncio.run(monitor._act_file_task(n["params"]))
    slugs = [t["slug"] for t in repair.backlog()["tasks"]]
    assert len(slugs) == len(set(slugs)), f"duplicated: {slugs}"


def test_toggling_a_factor_does_not_wipe_what_the_crew_has_earned(fresh_db, no_spend):
    """A factor toggle is a change of lineup, not amnesia. Rebuilding the scene used to
    throw away the manager conversation, the deliberation memos, and — since agents learn —
    every association each specialist had proved."""
    _root_user()
    info = repair.ensure_team()
    from app.lifeworld import store
    w = store.load(info["world_id"])
    s = w.scene(info["room_id"])
    h = next(p for p in s.players() if p.name == "Correctness")
    for i in range(2):
        d = h.decisions.record(i, "505 from a host", "the env is wrong", "switch env")
        h.decisions.resolve(d.id, "good", says="a 505 here is the staging env")
    h.memory.semantic["work.tech"] = "the venv symlink matters"
    s.threads[0].setdefault("chats", {})["manager"] = [
        {"role": "user", "text": "what are you working on?", "ts": 1.0}]
    s.threads[0]["results"] = [{"recommendation": "an earlier memo"}]
    store.save(w)

    repair.set_factors([{"id": "speed", "enabled": False}])       # the lineup changes
    info2 = repair.ensure_team()
    w2 = store.load(info2["world_id"])
    s2 = w2.scene(info2["room_id"])
    assert len(s2.players()) == 5, "the lineup really did change"
    h2 = next(p for p in s2.players() if p.name == "Correctness")
    assert h2.decisions.recall("http:505") is not None, "it forgot what it had proved"
    assert h2.memory.semantic.get("work.tech"), "it forgot what it knew"
    assert s2.threads[0]["chats"]["manager"][0]["text"] == "what are you working on?", \
        "the conversation vanished"
    assert s2.threads[0]["results"], "the deliberation history vanished"
    # ...but the psyche IS re-seeded from the dials: that is the point of a rebuild
    assert h2.psyche.traits["conscientiousness"] > 0.9


def test_unattended_approval_only_runs_what_adds(fresh_db, no_spend, monkeypatch):
    """Stopping work is never automated. A rule misfiring at 3am must not be able to switch
    the crew off and leave you to find it that way in the morning."""
    from app import logs, monitor
    monkeypatch.setattr(logs, "ECHO", False)
    assert set(monitor.AUTO_SAFE) == {"file_task", "set_knob"}
    assert "pause_repair" not in monitor.AUTO_SAFE and "abort_task" not in monitor.AUTO_SAFE
    repair.toggle(True)
    monitor.set_auto(True)
    logs.error("sandbox", "escape", "wrote outside", files="x.py")     # proposes pause_repair
    logs.error("http", "dashboard_error", "x is not a function", page="#/improve")
    done = asyncio.run(monitor.sweep())
    assert repair.enabled() is True, "a stopping action must never run unattended"
    assert any("queued for the next sprint" in d["did"] for d in done)


def test_nothing_runs_unattended_until_root_asks(fresh_db, no_spend, monkeypatch):
    from app import logs, monitor
    monkeypatch.setattr(logs, "ECHO", False)
    monitor.set_auto(False)
    logs.error("http", "dashboard_error", "x is not a function")
    assert asyncio.run(monitor.sweep()) == []
    assert not repair.backlog().get("tasks")


def test_an_unattended_approval_leaves_the_same_trace_as_a_pressed_one(fresh_db, no_spend, monkeypatch):
    """"The platform did it while I was asleep" must never mean "and left no trace"."""
    from app import logs, monitor
    monkeypatch.setattr(logs, "ECHO", False)
    monitor.set_auto(True)
    logs.error("http", "dashboard_error", "boom", page="#/improve")
    asyncio.run(monitor.sweep())
    assert any(r["event"] == "monitor_action" for r in logs.rows()), "it must be on the record"
    assert any(d.get("state") == "approved" for d in monitor.decisions().values())


def test_the_switch_is_where_the_approving_happens():
    js = dashboard_js()
    assert 'id="rpAuto"' in js and "/api/logs/notices/auto" in js
    assert "always waits for you" in js, "the limit has to be stated where the switch is"


def test_a_task_cannot_burn_the_quota_across_restarts(fresh_db, no_spend, monkeypatch):
    """A restart resumes the sprint — correct — but starts a FRESH session for the same task.
    Sprint 14 reached five attempts that way, one per restart, and each one was a whole
    session off the quota with nothing to show."""
    monkeypatch.setattr(config, "AUTH_CONFIGURED", False)
    tuning.set("repair_max_attempts", 2)
    rec = {"no": 1, "started_at": time.time(), "scout": {}, "memo": None, "landed": 0,
           "failed": 0, "landed_files": [],
           "tasks": [{"slug": "big", "title": "A big one", "factor": "speed", "brief": "",
                      "acceptance": [], "branch": "", "status": "pending", "worktree": None,
                      "verification": None, "landed_sha": None, "attempts": 2, "evidence": ""}]}
    repair.save_sprint(rec)
    db.kv_set("repair:seq", 1)
    repair.toggle(True)
    repair.set_state(phase="build", sprint_no=1, task_idx=0)
    asyncio.run(repair.tick())
    t = repair.sprint(1)["tasks"][0]
    assert t["status"] == "failed" and "gave up after" in t["error"]
    assert repair.state()["task_idx"] == 1, "and the sprint moves on"


def test_the_crew_is_shown_the_work_it_keeps_over_scoping(fresh_db):
    """"extract a router from routes.py" died on turns three sprints running. Showing the
    crew its own oversized tasks is cheaper and far more specific than prompt adjectives."""
    db.kv_set("repair:sprint:9", {"no": 9, "tasks": [
        {"title": "Extract first domain router from routes.py", "too_big": True},
        {"title": "A small fix that worked", "status": "landed"}]})
    assert repair._recent_too_big() == ["Extract first domain router from routes.py"]
    src = Path(repair.__file__).read_text()
    assert "do not propose work of this size" in src
    assert "is a PROGRAMME and will die unfinished" in repair.PLAN_EXTRACT_SYSTEM


def test_the_crew_world_keeps_exactly_one_sprint_table(fresh_db, no_spend):
    """Every rebuild used to materialise a new scene and abandon the old one, so the world
    accumulated a room per persona bump and per factor toggle — six of them, five dead, each
    still holding six agents. The world is one blob loaded and saved whole, so the dead rooms
    were being paid for on every tick."""
    _root_user()
    from app.lifeworld import store
    info = repair.ensure_team()
    for _ in range(3):                                    # three lineup changes
        repair.set_factors([{"id": "speed", "enabled": False}])
        repair.ensure_team()
        repair.set_factors([{"id": "speed", "enabled": True}])
        info = repair.ensure_team()
    w = store.load(info["world_id"])
    assert len(w.scenes) == 1, f"{len(w.scenes)} sprint tables piled up"
    s = w.scene(info["room_id"])
    assert s is not None and len(s.seats) == 6
    from app.lifeworld.human import Human
    people = [e for e in w.entities.values() if isinstance(e, Human)]
    assert len(people) == 6, f"{len(people)} agents left behind by old tables"
