"""Whether time or quality is the constraint — the input the boss never had.

From a real run: a brief asking for "a metroidvania like Hollow Knight" produced
six tasks, one sprint, six agent runs, and the entire game as ONE task written in
one pass, never sent back, never reviewed by anyone but the manager, never
verified by anything. The agents did what was asked. Nothing in the system ever
asked for more than a demo — and the agile guidance explicitly told the manager
not to build for anything nobody had requested.
"""

import pytest

from conftest import make_project, make_task
from app import ambition, db, launcher, tuning

from conftest import dashboard_js  # the split dashboard JS, concatenated in load order


def _project(level):
    pid = make_project(name=f"amb-{level}")
    db._execute("UPDATE projects SET ambition=? WHERE id=?", (level, pid))
    return db.get_project(pid)


def test_the_default_is_unchanged_behaviour(fresh_db):
    """Every project that existed before this must keep behaving as it did."""
    assert ambition.normalise(None) == "standard"
    assert ambition.knobs({"ambition": "standard"}) == {}
    assert ambition.worker_tier({"ambition": "standard"}) == ""


def test_an_unknown_level_falls_back_rather_than_failing(fresh_db):
    assert ambition.normalise("maximum") == "standard"


def test_exacting_says_time_is_not_the_constraint(fresh_db):
    """This is the whole point. Without saying it outright the manager optimises
    for finishing, because everything else about the system rewards that."""
    text = ambition.guidance({"ambition": "exacting"})
    assert "TIME IS NOT A CONSTRAINT" in text
    assert "finishing early with something thin is the failure mode" in text


def test_exacting_forbids_the_one_task_that_was_the_whole_project(fresh_db):
    """The actual defect from the game run: 'implement the game' as a single
    task, done once, by one agent."""
    text = ambition.guidance({"ambition": "exacting"})
    assert "'Implement the game' is a planning failure" in text
    assert "one task per meaningful piece" in text.lower()


def test_exacting_asks_for_the_parts_nobody_requests_by_name(fresh_db):
    """Error states, empty states and edge cases are what separate a demo from a
    product, and no brief has ever listed them."""
    text = ambition.guidance({"ambition": "exacting"})
    for thing in ("Error states", "edge cases", "performance"):
        assert thing in text


def test_draft_still_says_rough_is_fine(fresh_db):
    """The setting has to be able to mean 'less', or it is not a trade."""
    text = ambition.guidance({"ambition": "draft"})
    assert "fine for this to be rough" in text


# --- what it actually changes ---------------------------------------------

def test_exacting_starts_on_the_stronger_model_rather_than_arriving_there(fresh_db):
    """A cheap first attempt is not a saving here: the failure costs a full run
    and so does the retry, so you pay twice to end up where you started."""
    from app import config
    proj = _project("exacting")
    tid = make_task(proj["id"], role="backend", title="x")
    assert launcher.pick_model(db.get_task(tid), proj) == config._resolve_model("lead")

    plain = _project("standard")
    tid2 = make_task(plain["id"], role="backend", title="x")
    assert launcher.pick_model(db.get_task(tid2), plain) != config._resolve_model("lead")


def test_exacting_escalates_after_one_failure_not_two(fresh_db):
    from app import config
    proj = _project("exacting")
    tid = make_task(proj["id"], role="backend", title="x")
    db.update_task(tid, attempts=1, model=config._resolve_model("lead"))
    picked = launcher.pick_model(db.get_task(tid), proj)
    assert picked != config._resolve_model("lead"), "it did not move up after one failure"


def test_exacting_puts_more_than_one_reader_on_finished_work(fresh_db):
    assert ambition.get({"ambition": "exacting"}, "review_panel_size", 1) == 2
    assert ambition.get({"ambition": "draft"}, "review_panel_size", 1) == 0


def test_draft_switches_contests_off_entirely(fresh_db):
    assert ambition.get({"ambition": "draft"}, "contest_max_width", 3) == 1
    assert ambition.get({"ambition": "exacting"}, "contest_max_width", 3) == 3


def test_an_operators_global_tuning_is_not_silently_reverted(fresh_db):
    """Overrides, not assignments. Someone who tuned a knob deliberately should
    not have it undone by a per-project choice made for a different reason."""
    tuning.set("review_panel_size", 4)
    try:
        # standard declares no override, so the operator's value stands.
        assert ambition.get({"ambition": "standard"}, "review_panel_size",
                            int(tuning.get("review_panel_size"))) == 4
    finally:
        tuning.reset("review_panel_size")


def test_every_level_states_what_it_costs_you(fresh_db):
    """A dial whose ends are 'good' and 'bad' is not a trade. Each end has to say
    what you are giving up to be there."""
    entries = {e["id"]: e for e in ambition.catalog()}
    assert set(entries) == {"draft", "standard", "exacting"}
    assert all(e["when"] for e in entries.values())
    assert "longer" in entries["exacting"]["when"] and "cost" in entries["exacting"]["when"]
    assert entries["standard"]["default"] is True


def test_a_project_records_the_setting_it_was_created_with(fresh_db):
    proj = _project("exacting")
    assert proj["ambition"] == "exacting"
    assert "TIME IS NOT A CONSTRAINT" in ambition.guidance(proj)


# --- the choice reaches the project ----------------------------------------

def test_the_form_offers_the_choice_and_sends_it(fresh_db):
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "dashboard"
    html = (root / "index.html").read_text()
    js = dashboard_js()
    # A slider, not three tiles: the choice is a continuum in the reader's head,
    # and five tall blocks of prose made the step read as a form to survive.
    assert 'id="ambitionRange"' in html
    assert 'name="ambition"' in html          # the value the form still submits
    assert "Quality or speed?" in html
    assert 'ambition: f.get("ambition")' in js
    assert "AMBITION_STOPS" in js
    # Every stop names what CHANGES at that position — otherwise a slider only
    # moves a word and the trade stays a vibe.
    for stop in ("draft", "standard", "exacting"):
        assert f'id: "{stop}"' in js
    assert "changes:" in js


def test_creating_a_project_records_the_choice(fresh_db, root_client, root_can_run_agents):
    r = root_client.post("/api/projects", json={
        "name": "exacting one", "brief": "b", "ambition": "exacting"})
    assert db.get_project(r.json()["id"])["ambition"] == "exacting"


def test_an_unknown_value_from_a_client_does_not_create_a_broken_project(
        fresh_db, root_client, root_can_run_agents):
    r = root_client.post("/api/projects", json={
        "name": "typo", "brief": "b", "ambition": "maximum"})
    assert db.get_project(r.json()["id"])["ambition"] == "standard"


def test_the_slider_and_the_switch_replace_five_tiles(fresh_db):
    """Autonomy is binary so it is a switch; quality-versus-time is a continuum so
    it is a slider. Shapes that match the choice, instead of five identical tall
    cards each showing its own paragraph whether or not it was selected."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "dashboard"
    html = (root / "index.html").read_text()
    css = (root / "style.css").read_text()
    assert 'class="seg"' in html and 'id="autonomySeg"' in html
    assert "acard" not in html, "the old tiles are still in the markup"
    assert "acard" not in css, "dead rules for markup that no longer exists"


def test_the_slider_is_reachable_without_a_mouse(fresh_db):
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "dashboard"
    html = (root / "index.html").read_text()
    css = (root / "style.css").read_text()
    assert 'aria-label="How good does it have to be"' in html
    assert 'aria-describedby="ambitionSays"' in html
    assert "focus-visible" in css
