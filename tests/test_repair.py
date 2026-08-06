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
    usage.note("manager", "claude-opus-5", usd=0.4, ts=now - 30)
    ok, why, wake = repair.headroom(now)
    assert not ok and "your own work" in why
    assert wake > now, "a yield must name a time to check back"


def test_the_crew_does_not_yield_to_its_own_spending(fresh_db, no_spend):
    """The crew's deliberation runs through the same provider path a Studio seat does. Filed
    as the owner's, its own spend would read as 'someone else is using the quota' — and it
    would put itself to sleep forever, one tick after starting."""
    from app import usage
    now = time.time()
    usage.note("repair", "claude-sonnet-5", usd=2.0, ts=now - 10)
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
    budget = float(tuning.get("usage_budget_usd"))
    share = float(tuning.get("repair_idle_share"))
    assert usage.snapshot(now)["allowance_usd"] == pytest.approx(budget * share, rel=1e-3)
    usage.note("worker", "claude-sonnet-5", usd=budget * share, ts=now - 60)
    u = usage.snapshot(now)
    assert u["allowance_usd"] == 0, "the owner's work must take the room back"
    assert u["idle_frac"] < 1.0


def test_a_quiet_hour_stops_counting_as_contention(fresh_db, no_spend):
    from app import usage
    now = time.time()
    quiet = int(tuning.get("repair_yield_quiet_s"))
    usage.note("manager", "claude-opus-5", usd=0.01, ts=now - quiet - 60)
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
    usage.note("repair", "claude-sonnet-5", usd=0.02, ts=now - 60)
    ok, why, _ = repair.headroom(now)
    assert ok, f"a measured-idle window must beat the proxy: {why}"
    # ...and a measured-FULL window still stops it
    usage.note("repair", "claude-sonnet-5",
               usd=float(tuning.get("usage_budget_usd")) * float(tuning.get("repair_idle_share")),
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
    assert usage.snapshot(now)["repair_usd"] == pytest.approx(1.25)
    assert usage.backfill_repair() == 0
    assert usage.snapshot(now)["repair_usd"] == pytest.approx(1.25), "must not double-count"


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
