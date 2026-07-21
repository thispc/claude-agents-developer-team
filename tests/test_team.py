"""Teammates that persist across tasks, instead of a fresh stranger every time."""

import json

import pytest

from conftest import make_project, make_task
from app import db, team


def _roster(*members):
    return [{"role": r, "count": c, **rest} for r, c, rest in members]


def test_hiring_turns_head_counts_into_named_people(fresh_db):
    pid = make_project(name="t1")
    team.hire(pid, [{"role": "backend", "count": 2}, {"role": "tester", "count": 1}])

    people = db.list_agents(pid)
    assert [a["role"] for a in people] == ["backend", "backend", "tester"]
    assert len({a["name"] for a in people}) == 3      # nobody shares a name
    assert all(a["name"] for a in people)


def test_hiring_twice_does_not_double_the_payroll(fresh_db):
    """Recruiting runs on project creation AND whenever the boss edits the team."""
    pid = make_project(name="t2")
    roster = [{"role": "backend", "count": 2}]
    team.hire(pid, roster)
    team.hire(pid, roster)
    assert len(db.list_agents(pid)) == 2


def test_growing_a_role_hires_only_the_difference(fresh_db):
    pid = make_project(name="t3")
    team.hire(pid, [{"role": "backend", "count": 1}])
    first = db.list_agents(pid)[0]["name"]

    team.hire(pid, [{"role": "backend", "count": 3}])
    people = db.list_agents(pid, "backend")
    assert len(people) == 3
    assert people[0]["name"] == first     # the existing teammate is not replaced


def test_a_retry_goes_back_to_whoever_started_it(fresh_db):
    """A retry handed to someone else throws away everything the first attempt
    learned — the exact waste that carrying prior context forward exists to stop."""
    pid = make_project(name="t4")
    team.hire(pid, [{"role": "backend", "count": 3}])
    tid = make_task(pid, role="backend", title="build the API")

    first = team.claim(db.get_task(tid))
    team.release(db.get_task(tid), "did half of it")
    again = team.claim(db.get_task(tid))
    assert again["id"] == first["id"]


def test_work_spreads_across_the_role_rather_than_landing_on_one_person(fresh_db):
    pid = make_project(name="t5")
    team.hire(pid, [{"role": "backend", "count": 3}])

    claimed = []
    for i in range(3):
        tid = make_task(pid, role="backend", title=f"task {i}")
        claimed.append(team.claim(db.get_task(tid))["id"])
    assert len(set(claimed)) == 3     # three tasks, three different people


def test_a_teammate_carries_forward_only_accepted_work(fresh_db):
    """Rejected work must not become 'what you already built'. An agent told it
    built something the manager threw out would build on a decision nobody kept."""
    pid = make_project(name="t6")
    team.hire(pid, [{"role": "backend", "count": 1}])
    tid = make_task(pid, role="backend", title="auth")

    agent = team.claim(db.get_task(tid))
    team.release(db.get_task(tid), "Added JWT auth in auth.py", accepted=False)
    assert db.get_agent(agent["id"])["notes"] == ""
    assert db.get_agent(agent["id"])["tasks_done"] == 0

    team.claim(db.get_task(tid))
    team.release(db.get_task(tid), "Added JWT auth in auth.py", accepted=True)
    after = db.get_agent(agent["id"])
    assert "auth" in after["notes"]
    assert after["tasks_done"] == 1


def test_memory_is_capped_so_it_cannot_crowd_out_the_task(fresh_db):
    """The failure mode of 'remember everything' is a context window that grows
    until the actual task is the smallest thing in it."""
    pid = make_project(name="t7")
    team.hire(pid, [{"role": "backend", "count": 1}])
    tid = make_task(pid, role="backend", title="x")
    agent = team.claim(db.get_task(tid))

    for i in range(60):
        db.update_task(tid, title=f"task number {i}")
        team.claim(db.get_task(tid))
        team.release(db.get_task(tid), f"built the thing for iteration {i} " + "y" * 150,
                     accepted=True)

    notes = db.get_agent(agent["id"])["notes"]
    assert len(notes) <= team.NOTES_LIMIT
    assert "iteration 59" in notes        # the newest is kept
    assert "iteration 0 " not in notes    # the oldest is dropped


def test_identity_and_persona_reach_the_agent_prompt(fresh_db):
    pid = make_project(name="t8")
    team.hire(pid, [{"role": "backend", "count": 1,
                     "persona": "You are allergic to premature abstraction."}])
    agent = db.list_agents(pid)[0]

    text = team.system_addendum(agent)
    assert agent["name"] in text
    assert "premature abstraction" in text
    # A persona modifies the role prompt; it must not be able to revoke it.
    assert "does not override" in text


def test_no_team_means_the_old_behaviour_untouched(fresh_db):
    """Every project created before teammates existed has no agent rows, and a
    migration that broke those to add a feature would be a bad trade."""
    pid = make_project(name="t9")
    tid = make_task(pid, role="backend", title="x")
    assert team.claim(db.get_task(tid)) is None
    assert team.system_addendum(None) == ""
    assert team.model_for(None, "claude-haiku-4-5") == "claude-haiku-4-5"
    team.release(db.get_task(tid), "report")     # must not raise


def test_a_teammates_own_model_wins_over_the_platform_default(fresh_db):
    """This is what makes per-role model choice real rather than decorative."""
    pid = make_project(name="t10")
    team.hire(pid, [{"role": "researcher", "count": 1, "model": "claude-opus-4-8"}])
    agent = db.list_agents(pid)[0]
    assert team.model_for(agent, "claude-haiku-4-5") == "claude-opus-4-8"


def test_a_persona_can_be_changed_mid_project(fresh_db):
    pid = make_project(name="t11")
    team.hire(pid, [{"role": "backend", "count": 1, "persona": "terse"}])
    agent = db.list_agents(pid)[0]

    updated = team.set_persona(agent["id"], "explains every decision at length")
    assert "at length" in updated["persona"]
    assert "terse" not in team.system_addendum(db.get_agent(agent["id"]))


def test_the_manager_sees_people_not_head_counts(fresh_db):
    pid = make_project(name="t12")
    team.hire(pid, [{"role": "backend", "count": 2}])
    text = team.roster_text(pid)
    for a in db.list_agents(pid):
        assert a["name"] in text
    assert "backend" in text


def test_a_blueprint_roster_keeps_the_personas_the_table_argued_about(fresh_db):
    """These used to be discarded at exactly the moment they mattered."""
    bp = json.dumps({"team": [
        {"role": "Data Engineer", "count": 2, "persona": "distrusts schemas they did not write"},
        {"role": "tester"},
    ]})
    roster = team.from_blueprint(bp)
    assert roster[0]["persona"] == "distrusts schemas they did not write"
    assert roster[0]["count"] == 2
    assert roster[1]["count"] == 1


def test_an_unparseable_blueprint_yields_no_team_rather_than_an_error(fresh_db):
    """A model-written blueprint drifts in shape; a bad one must not stop a
    project being created."""
    assert team.from_blueprint("not json at all") == []
    assert team.from_blueprint(json.dumps({"team": "wrong type"})) == []
    assert team.from_blueprint(json.dumps({"team": [{"count": 3}]})) == []   # no role


# --- what a dispatch actually runs on -------------------------------------
#
# Caught by a live run on staging, not by this suite. A teammate's model was
# taken unconditionally, which broke two things at once and looked like one.

def test_a_roster_tier_is_resolved_to_a_real_model_id(fresh_db):
    """The roster speaks in tiers. Passing "worker" through as a model id reaches
    the vendor as a model literally called "worker" and kills the task — which is
    exactly what happened on the first live run."""
    from app import config, launcher
    pid = make_project(name="m1")
    team.hire(pid, [{"role": "backend", "count": 1, "model": "worker"}])
    agent = db.list_agents(pid)[0]
    tid = make_task(pid, role="backend", title="x")

    assert team.model_for(agent, "fallback") == config.WORKER_MODEL
    assert launcher.pick_model(db.get_task(tid), db.get_project(pid), agent) \
        == config.WORKER_MODEL


def test_a_managers_reassignment_beats_the_teammates_own_model(fresh_db):
    """The manager reassigned a failing task to sonnet-5 and the dispatch sent the
    teammate's model again, so it failed the same way twice. Whose model it is
    answers a different question from what to do when that model is not working."""
    from app import launcher
    pid = make_project(name="m2")
    team.hire(pid, [{"role": "backend", "count": 1, "model": "claude-haiku-4-5"}])
    agent = db.list_agents(pid)[0]
    tid = make_task(pid, role="backend", title="x")
    db.update_task(tid, pinned_model="claude-sonnet-5")

    assert launcher.pick_model(db.get_task(tid), db.get_project(pid), agent) \
        == "claude-sonnet-5"


def test_escalation_still_happens_for_someone_with_their_own_model(fresh_db):
    """Otherwise a teammate with a pinned cheap model retries on it forever."""
    from app import launcher
    pid = make_project(name="m3")
    team.hire(pid, [{"role": "backend", "count": 1, "model": "claude-haiku-4-5"}])
    agent = db.list_agents(pid)[0]
    tid = make_task(pid, role="backend", title="x")
    db.update_task(tid, attempts=2, model="claude-haiku-4-5")

    picked = launcher.pick_model(db.get_task(tid), db.get_project(pid), agent)
    assert picked != "claude-haiku-4-5"


def test_a_teammates_model_still_wins_over_the_role_default(fresh_db):
    """The point of per-role choice, which must survive the fix above."""
    from app import launcher
    pid = make_project(name="m4")
    team.hire(pid, [{"role": "backend", "count": 1, "model": "claude-opus-4-8"}])
    agent = db.list_agents(pid)[0]
    tid = make_task(pid, role="backend", title="x")

    assert launcher.pick_model(db.get_task(tid), db.get_project(pid), agent) \
        == "claude-opus-4-8"
