"""The drill: the six specialists actually do the work, and it can be proven offline.

Before this file existed, no test exercised deliberate → task → build → outcome → learning;
`note_decision`/`note_outcome` had zero test hits, and the claim "the crew does self-repair"
was true only of the planning phase. Everything here runs with injected fakes and asserts
call counts — the house style for policing spend invariants without a network.
"""

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from conftest import dashboard_js  # noqa: F401  (parity with sibling suites)
from app import auth, config, db, knowledge, repair, tuning
from app import repair_builder as rb


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from app import launcher
    launcher.COOLDOWN.clear()
    yield
    launcher.COOLDOWN.clear()


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


def _root_user():
    return auth.get_user_by_name(auth.ROOT_USERNAME) or auth.create_user(
        auth.ROOT_USERNAME, "pw-root", is_root=True)


def _task(factor="correctness", title="Fix the venv symlink in fresh worktrees",
          brief="builds fail with ImportError because the worktree has no .venv symlink"):
    return {"slug": repair._slug(title), "title": title, "factor": factor, "brief": brief,
            "type": "bug", "priority": "p2", "acceptance": [], "branch": "",
            "status": "pending", "worktree": None, "verification": None,
            "landed_sha": None, "attempts": 0, "evidence": ""}


def _go_live(monkeypatch):
    """Live enough for the specialist paths, with zero real spend possible."""
    monkeypatch.setattr(config, "AUTH_CONFIGURED", True)
    monkeypatch.setattr(repair, "_root_settings", lambda: {})


async def _run_sprint(monkeypatch, tmp_repo, tasks, prompts, review=False):
    """Seed the backlog with `tasks`, run ticks until the engine rests. The SDK session is a
    fake that records the system prompt it was handed and makes a real green-able change."""
    tuning.set("repair_review", review)
    tuning.set("repair_tasks_per_sprint", len(tasks))

    async def fake_sdk(system_prompt, prompt, cwd, tools, max_turns, model, env, **kw):
        prompts.append({"system": system_prompt, "tools": list(tools), "kw": kw})
        # UNIQUE content per session: sprint 2's worktree branches from a main that already
        # holds sprint 1's file, and an identical write is correctly rejected as "the
        # session changed nothing".
        (Path(cwd) / "fixed.txt").write_text(f"done {len(prompts)}\n")
        return "done", 0.0
    monkeypatch.setattr(rb, "_run_sdk", fake_sdk)

    async def fake_verify(wt):
        return {"ran": True, "ok": True, "headline": "42 passed", "failures": []}
    monkeypatch.setattr(rb, "verify", fake_verify)

    repair.save_backlog({"ts": time.time(), "digest": "seeded", "tasks": list(tasks)})
    repair.toggle(True)
    # The previous sprint ends in a deliberate PAUSE, which waits out its clock by design —
    # model the five minutes having passed, or the "second sprint" is sprint 1's record and
    # every assertion quietly tests the wrong thing.
    st = repair.state()
    if st.get("phase") == "sleeping":
        repair.set_state(phase="idle", sleep_until=0, sleep_reason="", sleep_kind="",
                         resume_phase="")
    for _ in range(24):
        await repair.tick()
        st = repair.state()
        if st["phase"] == "sleeping":
            break
    repair.toggle(False)
    return repair.sprint(int(db.kv_get("repair:seq") or 0))


# --------------------------------------------------------------------------
# the loop, twice: identity → work → outcome → the next build knows
# --------------------------------------------------------------------------

def test_a_specialist_builds_in_its_own_voice_and_learns(fresh_db, tmp_repo, monkeypatch):
    _root_user(); _go_live(monkeypatch)
    prompts = []

    rec1 = asyncio.run(_run_sprint(monkeypatch, tmp_repo, [_task()], prompts))
    assert rec1 and rec1["tasks"][0]["status"] == "landed", rec1 and rec1["tasks"]

    sys1 = prompts[0]["system"]
    assert "You are Correctness" in sys1, "the build must be the specialist's session"
    assert "conscientiousness 0.95" in sys1, "its defining traits are who it is"
    assert "you most want safety" in sys1
    assert "YOUR EXPERIENCE" not in sys1, "an empty store must confer no fake authority"
    assert rb.BUILDER_SYSTEM in sys1, "the ground rules ride along unchanged"

    # the outcome landed on the specialist, in both stores. Read back THROUGH the
    # boundary (repair.decision_node → the lifeworld service), not by opening a world
    # blob: since P4 the decision lives in another process, and a check that reached
    # around the wire would stop proving the thing it is here to prove.
    info = repair.team()
    wid, h = repair._specialist("correctness")
    node = repair.decision_node("correctness", rec1["tasks"][0]["decision_id"])
    assert node is not None and node["outcome"] == "good"
    owner = repair.reg_key_for(info["world_id"], h.id)
    rows = [r for r in knowledge.stats(owner)["rows"]]
    assert rows and sum(r["n"] for r in rows) >= 1, "the outcome never reached the knowledge base"
    assert any(r["event"] == "specialist_briefed" for r in __import__("app.logs", fromlist=["rows"]).rows())

    # SECOND task, similar situation: the brief now carries the proven lesson
    rec2 = asyncio.run(_run_sprint(
        monkeypatch, tmp_repo,
        [_task(title="Worktree ImportError strikes again",
               brief="a fresh worktree fails with ImportError, likely the venv symlink")],
        prompts))
    assert rec2["tasks"][0]["status"] == "landed"
    sys2 = prompts[-1]["system"]
    assert "YOUR EXPERIENCE" in sys2, "the second build must know what the first proved"
    assert "held up 1/1" in sys2, "and say exactly how proven it is"
    assert "the change held up" in sys2


def test_a_missing_specialist_builds_anonymously(fresh_db, tmp_repo, monkeypatch):
    """An unknown factor must not break anything — it gets today's anonymous build,
    byte-identical."""
    _root_user(); _go_live(monkeypatch)
    prompts = []
    rec = asyncio.run(_run_sprint(monkeypatch, tmp_repo, [_task(factor="no-such-lens")], prompts))
    assert rec["tasks"][0]["status"] == "landed"
    assert prompts[0]["system"] == rb.BUILDER_SYSTEM


# --------------------------------------------------------------------------
# consults: the arrows are the org chart, enforced
# --------------------------------------------------------------------------

def _room_log(info) -> list[dict]:
    """The crew room's log, as the canvas reads it.

    Through `lifeworld_client`, deliberately: the world blob belongs to the lifeworld
    service since P4, and a drill that opened it directly would stop testing the thing
    it exists to test — that a consult really lands in the room, across the wire.
    """
    from app import lifeworld_client
    got = lifeworld_client.room_view(info["world_id"], info["room_id"])
    return (got or {}).get("log") or []


def _counting_complete(replies):
    calls = []

    async def fake(provider, model, system, prompt, settings, max_tokens=2000):
        calls.append({"system": system, "prompt": prompt})
        return replies[min(len(calls) - 1, len(replies) - 1)]
    return fake, calls


def test_a_consult_only_reaches_a_graph_neighbour(fresh_db, monkeypatch):
    """Correctness's ring neighbours are Simplicity and Speed. Naming anyone else is refused
    with the real names — free, and the arrows stay the org chart."""
    _root_user(); _go_live(monkeypatch)
    repair.ensure_team()
    from app import providers
    fake, calls = _counting_complete(["should never be called"])
    monkeypatch.setattr(providers, "complete", fake)
    t = _task()
    out = asyncio.run(repair.consult(t, "how do I fix this import?", who="Seamlessness"))
    assert "Simplicity" in out and "Speed" in out, out
    assert "Seamlessness" not in out.split("Yours are:")[0] or "only consult" in out
    assert calls == [], "a refusal must be free"
    assert t.get("consults", []) == [], "and uncounted"


def test_a_consult_picks_the_neighbour_that_knows(fresh_db, monkeypatch):
    """Unnamed consults go to the neighbour with PROVEN lessons about the question — the one
    worth a bounded call — and the exchange is real: one model call, a say line in the crew
    room, and the answerer genuinely heard the ask."""
    _root_user(); _go_live(monkeypatch)
    info = repair.ensure_team()
    _wid, speed = repair._specialist("speed")
    asyncio.run(knowledge.remember(
        repair.reg_key_for(info["world_id"], speed.id),
        "ImportError in a fresh worktree during a build",
        "symlink .venv into every worktree or nothing imports", good=2))
    from app import providers
    fake, calls = _counting_complete(["symlink the venv — we proved this in sprint 3"])
    monkeypatch.setattr(providers, "complete", fake)

    t = _task()
    out = asyncio.run(repair.consult(t, "the worktree build dies with ImportError, why?"))
    assert "symlink" in out
    assert len(calls) == 1, "one consult = one bounded call"
    assert t["consults"][0]["who"] == "Speed", t["consults"]
    # the exchange is on the record everywhere it should be. The room log is read the
    # way the canvas reads it — the room view — because the world blob belongs to the
    # lifeworld service now and a test that opened it would be testing the wrong thing.
    log = _room_log(info)
    assert any("(consult)" in r["text"] for r in log), "the ask never reached the room"
    assert any(r["frm"] == speed.id and "symlink" in r["text"]
               for r in log if r["kind"] == "say"), "the answer is not in the room log"
    events = [e for e in db.list_events(0, limit=50) if e["kind"] == "repair_consult_reply"]
    assert events, "no consult_reply event for the activity feed"


def test_consults_cap_out_free(fresh_db, monkeypatch):
    _root_user(); _go_live(monkeypatch)
    repair.ensure_team()
    tuning.set("repair_consults_per_task", 2)
    from app import providers
    fake, calls = _counting_complete(["answer one", "answer two"])
    monkeypatch.setattr(providers, "complete", fake)
    t = _task()
    asyncio.run(repair.consult(t, "first question about the ImportError"))
    asyncio.run(repair.consult(t, "second question about the symlink"))
    out = asyncio.run(repair.consult(t, "third question, one too many"))
    assert "used all 2 consults" in out
    assert len(calls) == 2 and len(t["consults"]) == 2


def test_the_consult_tool_reaches_the_engine(fresh_db, tmp_repo, monkeypatch):
    """The build only grows the phone when there is a crew to call: with neighbours the tools
    gain ask_teammate and _run_sdk receives the server; anonymous builds get neither."""
    _root_user(); _go_live(monkeypatch)
    prompts = []
    asyncio.run(_run_sprint(monkeypatch, tmp_repo, [_task()], prompts))
    assert "mcp__crew__ask_teammate" in prompts[0]["tools"]
    assert "mcp_servers" in prompts[0]["kw"] and prompts[0]["kw"]["mcp_servers"]
    prompts.clear()
    asyncio.run(_run_sprint(monkeypatch, tmp_repo, [_task(factor="no-such")], prompts))
    assert "mcp__crew__ask_teammate" not in prompts[0]["tools"]
    assert "mcp_servers" not in prompts[0]["kw"]


# --------------------------------------------------------------------------
# review: one neighbour, one send-back, fail-open
# --------------------------------------------------------------------------

def test_review_can_send_a_green_task_back_once(fresh_db, tmp_repo, monkeypatch):
    """The suite proves the change WORKS; the neighbour judges whether it is the RIGHT
    change. One send-back, riding the red-suite retry plumbing; the second green pass
    approves without asking again."""
    _root_user(); _go_live(monkeypatch)
    from app import providers
    fake, calls = _counting_complete(
        ["SEND_BACK: the diff also edits app.py, which the task never asked for",
         "APPROVE"])
    monkeypatch.setattr(providers, "complete", fake)
    prompts = []
    rec = asyncio.run(_run_sprint(monkeypatch, tmp_repo, [_task()], prompts, review=True))
    t = rec["tasks"][0]
    assert t["status"] == "landed", t
    assert t.get("sent_back") is True, "the send-back never happened"
    assert len(prompts) == 2, "the task must have been rebuilt after the send-back"
    assert "FEEDBACK ON THE PREVIOUS ATTEMPT" not in prompts[0]["system"]
    # ONE review per task, total: the send-back consumed it, and the rebuild lands on green
    # alone. Reviewing every pass with only approvals left would be a call that can change
    # nothing — the cost cap is the design, not an accident.
    assert len(calls) == 1, "the consumed send-back must not buy a second review"
    ev = [e for e in db.list_events(0, limit=80) if e["kind"] == "repair_review"]
    assert any('"send_back"' in e["payload"] for e in ev)


def test_an_unparseable_review_approves(fresh_db, tmp_repo, monkeypatch):
    """Fail-open: a green suite must never be blocked by a reviewer's formatting."""
    _root_user(); _go_live(monkeypatch)
    from app import providers
    fake, calls = _counting_complete(["well, it looks broadly fine to me I suppose"])
    monkeypatch.setattr(providers, "complete", fake)
    prompts = []
    rec = asyncio.run(_run_sprint(monkeypatch, tmp_repo, [_task()], prompts, review=True))
    assert rec["tasks"][0]["status"] == "landed"
    assert not rec["tasks"][0].get("sent_back")
    assert len(prompts) == 1, "an unparseable verdict must not cost a rebuild"


# --------------------------------------------------------------------------
# the recalled-experience fix, and the knobs
# --------------------------------------------------------------------------

def test_recalled_experience_reaches_the_tier2_prompt(fresh_db):
    """perceive() pays for the lookup before every appraisal; until now no appraiser read
    it — the agent remembered, then thought from nothing anyway."""
    from app.lifeworld import appraise
    from app.lifeworld.types import Signal
    from app.lifeworld.world import World
    w = World(name="w")
    h = w.spawn_human("A")
    sig = Signal(kind="say", from_id=None, sense="hearing", intensity=0.7, stakes=0.5,
                 payload={"text": "another ImportError"}, domain="work.tech")
    seen = {}

    async def fake(provider, model, system, prompt, settings, max_tokens=2000):
        seen["prompt"] = prompt
        return '{"understood": "ok"}'
    ctx = {"recalled": {"says": "it is the venv symlink", "confidence": 0.8,
                        "seen": 4, "via": "exact"}}
    asyncio.run(appraise.model(h, sig, ctx, settings={}, complete=fake,
                               model_name="m", max_tokens=100))
    assert "YOU HAVE MET THIS BEFORE" in seen["prompt"]
    assert "it is the venv symlink" in seen["prompt"] and "0.8" in seen["prompt"]
    asyncio.run(appraise.model(h, sig, {}, settings={}, complete=fake,
                               model_name="m", max_tokens=100))
    assert "YOU HAVE MET THIS BEFORE" not in seen["prompt"], "no recall, no block"


def test_the_new_knobs_carry_their_rationales(fresh_db):
    for name in ("repair_consults_per_task", "repair_review"):
        assert len(tuning.KNOBS[name][3]) > 80, f"{name} has no real rationale"
    assert tuning.KNOBS["repair_review"][0] is True, "owner chose review-on"
    assert set(repair.SESSION_KINDS) >= {"consult", "review"}, "the meters must count them"


# --------------------------------------------------------------------------
# lessons from the first live sprint (41): the defence, and the visible diff
# --------------------------------------------------------------------------

def test_standing_by_a_reviewed_change_is_not_death(fresh_db, tmp_repo, monkeypatch):
    """Sprint 41, live: green build → send-back → the rebuild examined the feedback,
    concluded the change was already right, wrote nothing — and the engine failed the task,
    discarding green work. A branch that already holds commits plus a builder that stands by
    them is a defence, not a dead session. The suite ahead stays the hard gate."""
    _root_user(); _go_live(monkeypatch)
    from app import providers
    fake, calls = _counting_complete(
        ["SEND_BACK: prove the error handling is fixed, not relocated"])
    monkeypatch.setattr(providers, "complete", fake)
    prompts = []

    async def run():
        tuning.set("repair_review", True)
        tuning.set("repair_tasks_per_sprint", 1)
        writes = {"n": 0}

        async def fake_sdk(system_prompt, prompt, cwd, tools, max_turns, model, env, **kw):
            prompts.append({"system": system_prompt})
            writes["n"] += 1
            if writes["n"] == 1:                       # first build makes the change...
                (Path(cwd) / "fixed.txt").write_text("the fix\n")
            return "I stand by the change; the handler is genuinely fixed.", 0.0
        monkeypatch.setattr(rb, "_run_sdk", fake_sdk)   # ...the rebuild writes NOTHING

        async def fake_verify(wt):
            return {"ran": True, "ok": True, "headline": "green", "failures": []}
        monkeypatch.setattr(rb, "verify", fake_verify)
        repair.save_backlog({"ts": time.time(), "digest": "d", "tasks": [_task()]})
        repair.toggle(True)
        st = repair.state()
        if st.get("phase") == "sleeping":
            repair.set_state(phase="idle", sleep_until=0, sleep_reason="", sleep_kind="")
        for _ in range(24):
            await repair.tick()
            if repair.state()["phase"] == "sleeping":
                break
        repair.toggle(False)
        return repair.sprint(int(db.kv_get("repair:seq") or 0))
    rec = asyncio.run(run())
    t = rec["tasks"][0]
    assert t["status"] == "landed", (t.get("status"), t.get("error"))
    assert t.get("sent_back") is True
    assert len(prompts) == 2, "the rebuild must actually have run"
    from app import logs
    assert any(r["event"] == "stood_by_change" for r in logs.rows()), \
        "the defence must be on the record, not silent"


def test_the_reviewer_sees_the_actual_diff_not_only_the_stat(fresh_db):
    """Sprint 41's reviewer sent the change back with 'need the actual diff to verify' —
    because I only gave it file names and line counts."""
    import inspect
    src = inspect.getsource(repair._review_green)
    assert '"diff", "main...HEAD"' in src, "the review material must include the real diff"
    assert '"--stat"' in src, "and keep the stat as the summary"


def test_a_dead_room_pointer_heals_by_adoption_not_rebuild(fresh_db, monkeypatch):
    """The kv record is only a pointer, and it can lose a race the world file wins (two
    servers each saved their own idea of the world). Rebuilding on a dead pointer would
    re-seat new human ids and orphan every knowledge row keyed to the living ones — the
    crew's proven lessons above all. Adoption keeps the ids.

    Since P4 the seating happens in the lifeworld service, so this drill also proves the
    heal survives a PROCESS BOUNDARY: the pointer that goes stale is conductor kv, the
    room that survives is the service's, and the ids that must be preserved cross the
    wire in a JSON body."""
    _root_user()
    info = repair.ensure_team()
    assert info and repair._team_room_alive(info)
    old_ids = sorted(m["agent_id"] for m in repair.crew_members())
    # Corrupt the pointer the way the race did: a room that is nowhere on disk.
    bad = dict(info, room_id=info["room_id"] + 900,
               agents={k: v + 900 for k, v in info["agents"].items()})
    db.kv_set("repair:world", bad)
    assert repair._specialist("correctness")[1] is None, "the corruption must really un-staff"
    healed = repair.ensure_team()
    assert healed["room_id"] == info["room_id"], "must adopt the surviving room, not build anew"
    assert sorted(m["agent_id"] for m in repair.crew_members()) == old_ids, \
        "adoption must keep the human ids"
    assert repair._specialist("correctness")[1] is not None
    from app import logs
    assert any(r["event"] == "crew_room_adopted" for r in logs.rows())
