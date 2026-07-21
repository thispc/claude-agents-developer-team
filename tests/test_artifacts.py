"""What each sprint produced, kept after the next sprint has run over it.

The failure these cover is quiet: nothing errors when sprint 2 reworks a sprint-1
task, the earlier delivery simply stops being answerable. So most of these assert
that an old sprint still reads the way it read then, even once the rows behind it
say something else.
"""

import json

import pytest

from conftest import make_project, make_task
from app import artifacts, db, team


def _deliver(task_id, *, pr=None, report="", verified=True, cmd="pytest"):
    db.update_task(task_id, status="done", pr_number=pr, report=report,
                   verification=json.dumps({"ran": True, "ok": verified, "cmd": cmd}))


# --- freezing a sprint ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_snapshot_still_says_what_shipped_after_the_next_sprint_rewrites_it(fresh_db):
    """A task row is mutable. Sprint 2 reworking a sprint-1 task overwrites its
    status, report, PR and verification in place — which is how sprint 1's output
    used to disappear without anything failing."""
    pid = make_project(name="a1")
    tid = make_task(pid, title="the login page")
    _deliver(tid, pr=7, report="Built the login page")

    await artifacts.capture(pid, 1)
    db.advance_sprint(pid)
    db.update_task(tid, status="failed", pr_number=None, report="reverted in sprint 2")

    view = artifacts.read(pid, 1)
    assert view["frozen"] is True
    assert [i["title"] for i in view["delivered"]] == ["the login page"]
    assert view["delivered"][0]["pr"] == 7
    assert view["counts"]["failed"] == 0


@pytest.mark.asyncio
async def test_capturing_a_second_time_does_not_replace_history_with_a_later_reading(fresh_db):
    pid = make_project(name="a2")
    tid = make_task(pid, title="search")
    _deliver(tid)
    await artifacts.capture(pid, 1)

    db.update_task(tid, status="failed")
    await artifacts.capture(pid, 1)                     # the ordinary, lazy path
    assert artifacts.read(pid, 1)["counts"]["delivered"] == 1

    await artifacts.capture(pid, 1, force=True)         # only when explicitly asked
    assert artifacts.read(pid, 1)["counts"]["delivered"] == 0


@pytest.mark.asyncio
async def test_only_sprints_the_project_has_left_are_frozen(fresh_db):
    """Freezing the cycle still running would pin a description of half a sprint
    to the finished one."""
    pid = make_project(name="a3")
    make_task(pid, title="in flight")
    assert await artifacts.ensure(pid) == []

    db.advance_sprint(pid)
    assert await artifacts.ensure(pid) == [1]
    assert await artifacts.ensure(pid) == []            # idempotent


@pytest.mark.asyncio
async def test_the_last_sprint_of_a_finished_project_is_frozen_too(fresh_db):
    """Otherwise the final sprint of every completed project is the one sprint
    that never gets a record, because nothing ever advances past it."""
    pid = make_project(name="a4")
    _deliver(make_task(pid, title="ship it"))
    db.set_project_status(pid, "done")

    assert await artifacts.ensure(pid) == [1]
    assert artifacts.read(pid, 1)["frozen"] is True


def test_a_sprint_that_was_never_snapshotted_reads_live_and_admits_it(fresh_db):
    """Every project created before this existed has no snapshots, and a live read
    labelled as history would let a later edit pass for what shipped then."""
    pid = make_project(name="a5")
    _deliver(make_task(pid, title="old work"))

    view = artifacts.read(pid, 1)
    assert view["frozen"] is False
    assert view["counts"]["delivered"] == 1
    assert "may differ" in view["note"]


@pytest.mark.asyncio
async def test_the_timeline_marks_which_sprint_is_still_being_worked(fresh_db):
    pid = make_project(name="a6")
    _deliver(make_task(pid, title="one"))
    db.advance_sprint(pid)
    make_task(pid, title="two")
    await artifacts.ensure(pid)

    rows = artifacts.timeline(pid)
    assert [r["sprint"] for r in rows] == [1, 2]
    assert (rows[0]["frozen"], rows[0]["current"]) == (True, False)
    assert (rows[1]["frozen"], rows[1]["current"]) == (False, True)


@pytest.mark.asyncio
async def test_a_snapshot_keeps_the_name_of_whoever_did_the_work(fresh_db):
    """Agents get renamed, re-personad and hired over. A note that resolves to
    whoever holds that row today is a rewriting of history."""
    pid = make_project(name="a7")
    team.hire(pid, [{"role": "backend", "count": 1}])
    tid = make_task(pid, role="backend", title="api")
    who = team.claim(db.get_task(tid))["name"]
    _deliver(tid)
    await artifacts.capture(pid, 1)

    db.update_agent(db.list_agents(pid)[0]["id"], name="Somebody Else")
    assert artifacts.read(pid, 1)["delivered"][0]["agent"] == who


@pytest.mark.asyncio
async def test_deleting_a_project_takes_its_snapshots_with_it(fresh_db):
    pid = make_project(name="a8")
    _deliver(make_task(pid, title="x"))
    await artifacts.capture(pid, 1)
    db.delete_project(pid)
    assert db.list_sprint_artifacts(pid) == []


# --- release notes ----------------------------------------------------------

def test_every_line_of_the_recorded_notes_points_at_a_task(fresh_db):
    """A release note nobody can trace back to a task is a claim about your
    software with nothing underneath it."""
    pid = make_project(name="n1")
    a = make_task(pid, title="add search")
    b = make_task(pid, title="fix the importer")
    _deliver(a, pr=3, report="Added search across notes")
    db.update_task(b, status="failed")

    text = artifacts.recorded_notes(artifacts.read(pid, 1))
    assert "#1 add search" in text and "PR #3" in text
    assert "#2 fix the importer" in text
    assert "Failed" in text


def test_work_nothing_checked_is_not_reported_as_work_that_passed(fresh_db):
    """'Nothing verified this' is a third answer, and folding it into either of
    the other two is how unverified work comes to look verified."""
    pid = make_project(name="n2")
    _deliver(make_task(pid, title="checked"), verified=True)
    unchecked = make_task(pid, title="unchecked")
    db.update_task(unchecked, status="done")

    view = artifacts.read(pid, 1)
    assert view["counts"]["delivered"] == 2
    assert view["counts"]["verified"] == 1
    assert view["counts"]["unverified"] == 1
    assert "never checked" in artifacts.recorded_notes(view)


@pytest.mark.asyncio
async def test_notes_are_written_from_the_record_when_no_model_is_asked_for(fresh_db):
    """The floor has to work with no key and no network, or the only projects with
    release notes are the ones on a funded account."""
    pid = make_project(name="n3")
    _deliver(make_task(pid, title="the thing"))

    out = await artifacts.release_notes(pid, 1)
    assert out["source"] == "recorded"
    assert "the thing" in out["notes"]


@pytest.mark.asyncio
async def test_a_draft_that_cites_a_task_this_sprint_never_had_is_thrown_out(fresh_db,
                                                                             monkeypatch):
    """One invented citation discards the whole draft: a writer that fabricated a
    reference has told you what its other sentences are worth."""
    pid = make_project(name="n4")
    _deliver(make_task(pid, title="real work"))

    async def _draft(*a, **kw):
        return "Shipped #1 real work, and also #94 a whole payments system."
    monkeypatch.setattr(artifacts.providers, "complete", _draft)

    out = await artifacts.release_notes(pid, 1, {"anthropic_api_key": "k"},
                                        provider="anthropic", rewrite=True)
    assert out["source"] == "recorded"
    assert "94" in out["model_error"]
    assert "real work" in out["notes"]          # the record still gets through


@pytest.mark.asyncio
async def test_a_draft_that_stays_inside_the_record_is_kept_with_the_facts_beside_it(
        fresh_db, monkeypatch):
    pid = make_project(name="n5")
    _deliver(make_task(pid, title="real work"), pr=2)

    async def _draft(*a, **kw):
        return "Search now works end to end (#1)."
    monkeypatch.setattr(artifacts.providers, "complete", _draft)

    out = await artifacts.release_notes(pid, 1, {"anthropic_api_key": "k"},
                                        provider="anthropic", rewrite=True)
    assert out["notes"].startswith("Search now works")
    assert out["source"].startswith("anthropic:")
    assert out["cites"] == [1]
    # The prose never stands alone: the itemised record comes back with it.
    assert "PR #2" in out["recorded"]
    assert out["facts"]["delivered"][0]["seq"] == 1


@pytest.mark.asyncio
async def test_a_pull_request_number_is_not_read_as_an_invented_task(fresh_db, monkeypatch):
    """Failing an honest draft for quoting the record correctly is the one mistake
    that would teach a reader to ignore this check."""
    pid = make_project(name="n8")
    _deliver(make_task(pid, title="real work"), pr=64)

    async def _draft(*a, **kw):
        return "Search landed (#1, PR #64)."
    monkeypatch.setattr(artifacts.providers, "complete", _draft)

    out = await artifacts.release_notes(pid, 1, {"anthropic_api_key": "k"},
                                        provider="anthropic", rewrite=True)
    assert out["source"].startswith("anthropic:")
    assert "model_error" not in out


@pytest.mark.asyncio
async def test_a_draft_that_names_no_task_at_all_is_discarded_too(fresh_db, monkeypatch):
    """Prose anchored to nothing reads like a summary and gives a reader no way to
    find out it is wrong."""
    pid = make_project(name="n9")
    _deliver(make_task(pid, title="real work"))

    async def _draft(*a, **kw):
        return "This sprint delivered substantial improvements across the platform."
    monkeypatch.setattr(artifacts.providers, "complete", _draft)

    out = await artifacts.release_notes(pid, 1, {"anthropic_api_key": "k"},
                                        provider="anthropic", rewrite=True)
    assert out["source"] == "recorded"
    assert "named no task" in out["model_error"]


@pytest.mark.asyncio
async def test_an_unreachable_model_leaves_the_recorded_notes_standing(fresh_db,
                                                                       monkeypatch):
    pid = make_project(name="n6")
    _deliver(make_task(pid, title="real work"))

    async def _boom(*a, **kw):
        raise RuntimeError("no credentials for Claude")
    monkeypatch.setattr(artifacts.providers, "complete", _boom)

    out = await artifacts.release_notes(pid, 1, {"anthropic_api_key": "k"},
                                        provider="anthropic", rewrite=True)
    assert out["source"] == "recorded"
    assert "no credentials" in out["model_error"]


@pytest.mark.asyncio
async def test_notes_are_stored_only_once_the_sprint_they_describe_is_frozen(fresh_db):
    pid = make_project(name="n7")
    _deliver(make_task(pid, title="mid-flight"))

    await artifacts.release_notes(pid, 1)
    assert db.get_sprint_artifact(pid, 1) is None

    db.advance_sprint(pid)
    await artifacts.ensure(pid)
    await artifacts.release_notes(pid, 1)
    assert "mid-flight" in db.get_sprint_artifact(pid, 1)["notes"]


# --- per-teammate view ------------------------------------------------------

def test_a_teammates_output_is_their_tasks_regrouped_not_a_second_copy_of_them(fresh_db):
    """Artifacts belong to tasks. A separate per-agent store would be two records
    of one thing, free to disagree."""
    pid = make_project(name="g1")
    team.hire(pid, [{"role": "backend", "count": 2}])
    people = db.list_agents(pid)

    for i, agent in enumerate(people):
        tid = make_task(pid, role="backend", title=f"task {i}")
        db.update_task(tid, agent_id=agent["id"])
        _deliver(tid, pr=10 + i)

    view = artifacts.by_agent(pid)
    assert view["basis"] == "agent"
    assert {g["name"] for g in view["agents"]} == {a["name"] for a in people}
    assert all(g["delivered"] == 1 for g in view["agents"])
    assert sorted(sum((g["prs"] for g in view["agents"]), [])) == [10, 11]


def test_a_project_from_before_teammates_had_names_groups_by_role_instead(fresh_db):
    """One empty bucket would read as 'nobody did anything' — which is not what
    happened, it is only what the schema remembers."""
    pid = make_project(name="g2")
    _deliver(make_task(pid, role="backend", title="api"))
    _deliver(make_task(pid, role="tester", title="suite"))

    view = artifacts.by_agent(pid)
    assert view["basis"] == "role"
    assert {g["name"] for g in view["agents"]} == {"backend", "tester"}
    assert "grouped by role" in view["note"]


def test_a_teammates_view_can_be_narrowed_to_one_sprint(fresh_db):
    pid = make_project(name="g3")
    team.hire(pid, [{"role": "backend", "count": 1}])
    agent = db.list_agents(pid)[0]
    first = make_task(pid, role="backend", title="sprint one work")
    db.update_task(first, agent_id=agent["id"])
    _deliver(first)
    db.advance_sprint(pid)
    second = make_task(pid, role="backend", title="sprint two work")
    db.update_task(second, agent_id=agent["id"])
    _deliver(second)

    assert artifacts.by_agent(pid, 2)["agents"][0]["delivered"] == 1
    assert artifacts.by_agent(pid)["agents"][0]["delivered"] == 2


@pytest.mark.asyncio
async def test_a_teammates_view_of_a_frozen_sprint_agrees_with_the_frozen_record(fresh_db):
    """Two views of one sprint that disagree about what shipped are worse than one."""
    pid = make_project(name="g6a")
    team.hire(pid, [{"role": "backend", "count": 1}])
    agent = db.list_agents(pid)[0]
    tid = make_task(pid, role="backend", title="shipped then reverted")
    db.update_task(tid, agent_id=agent["id"])
    _deliver(tid)
    await artifacts.capture(pid, 1)
    db.advance_sprint(pid)
    db.update_task(tid, status="failed")

    view = artifacts.by_agent(pid, 1)
    assert view["agents"][0]["name"] == agent["name"]
    assert view["agents"][0]["delivered"] == 1


def test_rework_is_visible_per_teammate(fresh_db):
    """Who keeps being sent work they cannot finish first time is a briefing
    problem, and it is invisible in a project-wide average."""
    pid = make_project(name="g4")
    team.hire(pid, [{"role": "backend", "count": 1}])
    agent = db.list_agents(pid)[0]
    tid = make_task(pid, role="backend", title="hard one")
    db.update_task(tid, agent_id=agent["id"], attempts=3)
    _deliver(tid)

    assert artifacts.by_agent(pid)["agents"][0]["rework"] == 2


def test_one_teammates_work_reads_across_every_sprint_they_were_here_for(fresh_db):
    pid = make_project(name="g5")
    team.hire(pid, [{"role": "backend", "count": 1}])
    agent = db.list_agents(pid)[0]
    for sprint_no in (1, 2):
        tid = make_task(pid, role="backend", title=f"work {sprint_no}")
        db.update_task(tid, agent_id=agent["id"])
        _deliver(tid)
        db.advance_sprint(pid)

    view = artifacts.agent_view(pid, agent["id"])
    assert [s["sprint"] for s in view["sprints"]] == [2, 1]     # newest first
    assert view["name"] == agent["name"]
    assert artifacts.agent_view(pid, 9999) is None


def test_a_teammate_from_another_project_is_not_visible_here(fresh_db):
    """The lookup is by agent id, which is global — without the project check it
    would be a cross-project read for anyone who could guess a number."""
    mine = make_project(name="g6")
    theirs = make_project(name="g7", owner_id=2)
    team.hire(theirs, [{"role": "backend", "count": 1}])
    outsider = db.list_agents(theirs)[0]

    assert artifacts.agent_view(mine, outsider["id"]) is None


# --- routes -----------------------------------------------------------------

def test_the_sprint_routes_capture_on_read_and_refuse_other_peoples_projects(
        root_client, make_user):
    pid = make_project(owner_id=1, name="r1")
    _deliver(make_task(pid, title="shipped this"))
    db.advance_sprint(pid)

    r = root_client.get(f"/api/projects/{pid}/sprints")
    assert r.status_code == 200, r.text
    assert r.json()["captured_now"] == [1]
    assert r.json()["sprints"][0]["frozen"] is True

    notes = root_client.get(f"/api/projects/{pid}/sprints/1/notes").json()
    assert "shipped this" in notes["notes"]

    by_agent = root_client.get(f"/api/projects/{pid}/by-agent").json()
    assert by_agent["basis"] == "role"

    _uid, other = make_user("nosy")
    assert other.get(f"/api/projects/{pid}/sprints").status_code == 404
    assert other.get(f"/api/projects/{pid}/by-agent").status_code == 404
