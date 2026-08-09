"""How work gets broken up, and what survives the round table.

Both were accidents before. The platform had no process model, which did not mean
it had no process — it meant a DAG planned in one pass with dependencies wired by
role, which is waterfall in everything but name and was never a choice anyone
made. And the round table argued about what kind of team the idea needed, then
threw that judgement away and kept the head count.
"""

import json

import pytest

from conftest import make_project, make_task
from app import db, process, roundtable, team


# --- the process model ----------------------------------------------------

def test_the_default_is_agile_and_it_is_a_choice_now(fresh_db):
    assert process.normalise(None) == "agile"
    assert process.normalise("") == "agile"
    assert process.normalise("waterfall") == "waterfall"


def test_an_unknown_process_falls_back_rather_than_failing(fresh_db):
    """A typo in an API call must not create a project that cannot be planned."""
    assert process.normalise("kanban") == "agile"
    assert process.normalise("AGILE") == "agile"    # not a known key; falls back


def test_agile_tells_the_manager_to_slice_by_outcome(fresh_db):
    text = process.guidance({"process": "agile"})
    assert "OUTCOME" in text
    assert "RUNS" in text                            # first sprint must produce one


def test_waterfall_tells_the_manager_to_slice_by_layer(fresh_db):
    text = process.guidance({"process": "waterfall"})
    assert "LAYER" in text
    assert "contract" in text.lower()


def test_each_model_names_the_cost_it_is_accepting(fresh_db):
    """Guidance that only lists advantages is marketing. The manager has to know
    what it is trading away to notice when the trade stops being worth it."""
    assert "invalidates finished work" in process.guidance({"process": "waterfall"})
    assert "collide" in process.guidance({"process": "agile"})


def test_agile_warns_that_two_agents_on_one_file_is_not_parallelism(fresh_db):
    """This is the failure the platform actually hit: two workers on one branch
    overwrote each other's outcome."""
    assert "same file" in process.guidance({"process": "agile"})


def test_a_project_with_no_process_recorded_still_gets_guidance(fresh_db):
    """Every project created before this existed has an empty column."""
    assert process.guidance({}) == process.guidance({"process": "agile"})
    assert process.guidance({"process": ""})


def test_the_catalog_says_when_each_model_is_right(fresh_db):
    """So the choice in the UI is informed rather than a coin flip."""
    entries = {e["id"]: e for e in process.catalog()}
    assert set(entries) == {"agile", "waterfall"}
    assert all(e["when"] for e in entries.values())
    assert entries["agile"]["default"] is True


def test_a_project_records_the_process_it_was_created_with(fresh_db):
    pid = make_project(name="pr")
    db._execute("UPDATE projects SET process=? WHERE id=?", ("waterfall", pid))
    assert db.get_project(pid)["process"] == "waterfall"
    assert "LAYER" in process.guidance(db.get_project(pid))


# --- what the round table hands over --------------------------------------

def test_the_blueprint_asks_for_personas_not_just_head_counts(fresh_db):
    """The deliberation works out what kind of judgement the work needs. If only
    the count survives, the argument ends with the blueprint."""
    prompt = roundtable._synth_prompt("build a thing", "…transcript…")
    assert '"persona"' in prompt
    assert "from_seat" in prompt
    assert "standing instructions" in prompt


def test_a_persona_must_be_judgement_not_a_restated_role_name(fresh_db):
    """Without saying this, the personas come back as "the backend engineer does
    backend work", which is a head count wearing a longer sentence."""
    prompt = roundtable._synth_prompt("x", "y")
    assert "not a restatement of the role name" in prompt


def test_personas_from_a_blueprint_reach_the_people_who_are_hired(fresh_db):
    """End to end: the table's reasoning becomes a real teammate's instructions."""
    pid = make_project(name="bp")
    blueprint = json.dumps({"team": [
        {"role": "data_engineer", "count": 2,
         "persona": "assume every upstream feed lies until you have checked it"},
        {"role": "writer", "count": 1, "persona": "plain language, no hedging"},
    ]})
    team.hire(pid, team.from_blueprint(blueprint))

    people = {a["role"]: a for a in db.list_agents(pid)}
    assert "upstream feed lies" in people["data_engineer"]["persona"]
    assert "upstream feed lies" in team.system_addendum(people["data_engineer"])
    assert len(db.list_agents(pid, "data_engineer")) == 2


def test_a_blueprint_role_with_no_persona_still_hires_someone(fresh_db):
    """A model that ignores half the schema must not cost you the team."""
    pid = make_project(name="bp2")
    team.hire(pid, team.from_blueprint(json.dumps({"team": [{"role": "tester"}]})))
    assert len(db.list_agents(pid, "tester")) == 1
