"""The About page, and the one colour rule that carries meaning.

Both are source-level assertions rather than browser tests, because the suite has
no browser. That limit is worth stating: these prove the page is wired and the
palette is consistent, not that either looks right.
"""

from pathlib import Path

import pytest

from conftest import dashboard_js  # the split dashboard JS, concatenated in load order

DASH = Path(__file__).resolve().parent.parent / "dashboard"
HTML = (DASH / "index.html").read_text()
JS = dashboard_js()
CSS = (DASH / "style.css").read_text()

pytestmark = pytest.mark.hostonly   # reads the repo, not the running image


def test_the_about_page_exists_and_has_a_way_in():
    assert 'id="aboutPage"' in HTML
    assert 'id="aboutBtn"' in HTML
    assert '$("#aboutBtn").addEventListener' in JS


def test_about_has_its_own_address_so_it_can_be_linked():
    """Someone sending this to a colleague should be able to send the page, not
    'open the app and press the question mark'."""
    assert 'setHash("#/about")' in JS
    # the router resolves #/about somewhere in route() — position-independent, since new
    # screens (studio, scenes, lifeworld) legitimately add their own route branches ahead of it
    assert '#/about' in JS.split("function route()")[1].split("\nfunction ")[0]


def test_every_screen_hides_the_about_page():
    """A screen left visible underneath another is the classic single-page bug: it does not
    error, it just renders two pages on top of each other.

    Asserted against the shared helper rather than each function's body — six copies of one
    hide-list is how a NEW screen gets forgotten by half of them, which happened, so the list
    now lives in one place and this checks that place.
    """
    assert "#aboutPage" in JS.split("const SCREENS = [", 1)[1].split("]", 1)[0]
    for opener in ("function showHome", "async function openSelfRepair",
                   "function openProject", "async function openPlan"):
        body = JS.split(opener)[1][:700]
        assert "aboutPage" in body or "hideScreens(" in body, \
            f"{opener} neither hides the About page nor uses the shared helper"


def test_the_about_page_says_what_is_unfinished():
    """A page inside the product describing the product is exactly where an
    honest limitation is most likely to get quietly dropped."""
    assert "configurable, not supported" in HTML
    assert "unproven" in HTML
    # And the isolation claim is the corrected one, not the flattering one.
    assert "separate <em>process</em>, not a separate" in HTML


def test_brick_means_a_human_is_involved():
    """The palette rule that carries information rather than decoration: anything
    a human is needed for is brick, anything the machine does alone is pine. If
    --boss ever drifts back to an arbitrary hue, the feed stops answering 'is
    something waiting on me' at a glance."""
    assert "--boss: #A9503A" in CSS
    assert "--accent: #2E6E5B" in CSS
    assert "a human is involved" in CSS


def test_no_indigo_left_over_from_the_previous_theme():
    """The old accent and its tints were scattered as literals. Any survivor now
    reads as a stray purple in a green palette."""
    for stale in ("#4f46e5", "#4338ca", "#7c3aed", "#eef2ff", "#faf7ff", "#d9c9ff"):
        assert stale not in CSS, f"{stale} survived the retheme"


def test_the_about_page_reads_as_a_document_not_a_dashboard():
    """It is the one screen that is read top to bottom rather than operated, and
    the type is supposed to say so."""
    assert "--serif:" in CSS
    doc_rule = CSS.split(".doc {")[1][:220]
    assert "var(--serif)" in doc_rule
    assert "max-width" in doc_rule


def test_the_page_pulls_in_nothing_from_the_internet():
    """The pod has no guaranteed outbound access, and a silently missing font or
    script would degrade the one page meant to explain the product."""
    for tag in ("<script src=\"http", "<link rel=\"stylesheet\" href=\"http", "@import url(http"):
        assert tag not in HTML and tag not in CSS


# --- the page must agree with the code -------------------------------------
#
# Every one of these was wrong when the page was first written. Prose drifts from
# code silently, and a handbook that confidently states a stale number is worse
# than one that omits it — so the numbers are asserted against their source.

def _handbook() -> str:
    return (Path(__file__).resolve().parent.parent / "docs" / "HANDBOOK.md").read_text()


def test_the_endpoint_count_is_the_real_one():
    # routes.py became the app/routes/ package; the endpoints it counted are now
    # spread across the domain modules, so the count sums over every file in it.
    pkg = Path(__file__).resolve().parent.parent / "conductor" / "app" / "routes"
    actual = sum(f.read_text().count("@router.") for f in sorted(pkg.glob("*.py")))
    assert f"There are {actual}" in _handbook() or f"are {actual};" in _handbook(), \
        f"the handbook does not say {actual} endpoints"


def test_the_internal_door_really_has_three_requests_behind_it():
    """The page tells a reader that a teammate can do exactly three things. If a
    fourth is added, that sentence becomes a false security claim."""
    pkg = Path(__file__).resolve().parent.parent / "conductor" / "app" / "routes"
    routes = "\n".join(f.read_text() for f in sorted(pkg.glob("*.py")))
    internal = routes.count('@router.get("/internal/') + routes.count('@router.post("/internal/')
    assert internal == 4, (
        f"{internal} internal endpoints exist; the About page and handbook both say "
        "four, and both need updating")


def test_the_table_count_matches_the_schema():
    # knowledge's one table left with the P1 extraction: the knowledge SERVICE
    # owns it now (services/knowledge, data/knowledge.db), so it is no longer
    # part of the conductor's count — the honest accounting is 34 here plus a
    # named pointer at the service, and the handbook says exactly that. (The
    # legacy fallback still creates a knowledge table until commit B, but a
    # table on the way out is not a table the platform declares.)
    from app import db, auth, findings, modgraph
    declared = sum(mod.SCHEMA.count("CREATE TABLE IF NOT EXISTS")
                   for mod in (db, auth, findings, modgraph))
    assert "Thirty-four tables" in _handbook() and declared == 34, \
        f"{declared} tables are declared; the handbook says thirty-four"
    assert "knowledge service" in _handbook(), \
        "the handbook no longer says where the knowledge table went"


def test_the_round_table_range_matches_the_limits():
    from app import roundtable
    assert roundtable.MIN_SEATS == 3 and roundtable.MAX_SEATS == 8
    assert "Three to eight" in _handbook(), \
        "the handbook's seat range no longer matches MIN_SEATS/MAX_SEATS"


def test_escalation_wording_matches_the_knob():
    from app import tuning
    assert tuning.KNOBS["escalate_after_attempts"][0] == 2
    assert "Two failures escalate" in _handbook()


def test_the_handbook_does_not_claim_workers_are_containerised():
    """The deployment runs LAUNCHER=local, so workers are subprocesses sharing the
    conductor's container. The first draft claimed otherwise, which is the kind of
    error that reads as a security guarantee."""
    text = _handbook()
    assert "separate *process*, not a" in text
    assert "boxed into an isolated container" not in text


def test_the_stated_size_of_the_conductor_is_not_badly_stale():
    """Stated as "about 13,500 lines" — it was written as 5,000, which was wrong by
    more than a factor of two. A line count drifts with every commit, so this is a
    loose band rather than an exact match: it should fail when the number becomes
    misleading, not every time someone adds a function.
    """
    import re
    root = Path(__file__).resolve().parent.parent / "conductor"
    actual = sum(len(f.read_text().splitlines()) for f in root.rglob("*.py"))
    claimed = int(re.search(r"about ([\d,]+) lines", _handbook()).group(1).replace(",", ""))
    assert 0.75 * claimed <= actual <= 1.35 * claimed, (
        f"the handbook says about {claimed:,} lines; the conductor is now {actual:,}")


# --- the wait between "create" and "there are tasks" -----------------------

def test_the_first_wait_is_not_a_single_dead_sentence():
    """It was one centred grey line with no motion and no end, shown for the
    better part of a minute while the manager plans — indistinguishable from a
    hang, and the most common first impression of the product."""
    # Matched on the RENDERED markup, not the string anywhere in the file — the
    # comment above the replacement quotes the old placeholder to explain itself,
    # and a naive search flags that as the bug it is describing.
    assert '<div class="empty">Assembling' not in JS, "the dead placeholder is back"
    assert "function assemblingHtml" in JS


def test_the_wait_shows_the_team_that_is_about_to_exist():
    """The roster is already known when the boss presses create, so the shape of
    the team can be drawn before anyone is hired. An empty area with a sentence
    in it tells you nothing; five ghost cards tell you what is coming."""
    body = JS.split("function assemblingHtml")[1][:1600]
    assert "ghost-agent" in body
    assert "roster" in body


def test_the_wait_says_what_the_manager_is_actually_doing():
    """Its thinking is already streaming in. Using it beats a canned phrase,
    because a canned phrase is still there when the thing has died."""
    assert "assemblingHtml(p, roster, managerThought || managerThinking)" in JS


def test_the_wait_admits_when_it_has_gone_on_too_long():
    """'Still working' stops being a fair explanation at some point, and the
    honest answer names the thing to press rather than leaving someone guessing."""
    body = JS.split("function assemblingHtml")[1][:1600]
    assert "stalled" in body
    assert "Restart manager" in body


def test_the_waiting_animation_respects_reduced_motion():
    # There may be more than one reduced-motion block now (the Studio adds its
    # own); the pulse must be nulled in SOME block, not the last one.
    assert "prefers-reduced-motion" in CSS
    blocks = CSS.split("@media (prefers-reduced-motion: reduce)")[1:]
    assert any(".pulse" in b[:300] and "animation: none" in b[:300] for b in blocks)


# --- forms share one rhythm ------------------------------------------------

def test_form_metrics_come_from_tokens_not_from_each_context():
    """Controls were styled per context — the dialog said 9px, Settings 10px, the
    self-repair card 9px at a different font size, the base rule 7px. Nothing was
    wrong alone; together no two forms in the product lined up, and each new form
    invented a fourth answer."""
    for token in ("--ctl-pad-y", "--ctl-pad-x", "--field-gap", "--stack-gap",
                  "--label-size", "--ctl-size"):
        assert token in CSS, f"{token} is not defined"

    import re
    # Comments are stripped first, and the selector is matched on a word boundary.
    # Without both, a comment containing the word "selection" made the rule that
    # followed it look like a form-control rule, and the test failed on prose.
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    offenders = []
    for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        if not re.search(r"\b(input|textarea|select)\b", selector):
            continue
        # `padding: 0` is a reset — stripping the browser's own chrome off a
        # range input — not a spacing decision. Everything else must come from
        # a token, which is the thing this test exists to enforce.
        if re.search(r"padding:\s*(?!0\b)\d", body):
            offenders.append(f"{selector.strip()} {{{body.strip()}}}")
    assert not offenders, "form controls still hardcode padding:\n" + "\n".join(offenders[:4])


def test_the_old_cool_greys_are_gone():
    """Left over from the indigo palette. Each one reads as a blue-grey smudge in
    a warm, green-biased one."""
    for stale in ("#cfd4dd", "#d3d8e2", "#b9c0cd", "#aeb5c1"):
        assert stale not in CSS, f"{stale} survived the sweep"


def test_a_hint_sits_with_the_control_it_describes():
    """Equal spacing above and below made a hint read as a heading for the NEXT
    field rather than a note about the previous one."""
    assert "label > .hint, .field-hint" in CSS
    assert "margin-top: calc(var(--field-gap) * -0.5)" in CSS


# --- regressions reported from a live run ----------------------------------

def test_the_managers_model_is_the_headline_not_a_bare_dropdown():
    """Replacing the big model name with a select lost the one fact the card
    existed to show — and said the wrong thing: a manager running claude-fable-5
    displayed "server default", because the picker's list did not contain it and
    a select falls back to its first option."""
    assert 'id="mgrModelName"' in JS
    assert "function modelName(" in JS
    assert "function managerOptions(" in JS
    body = JS.split("function managerOptions(")[1][:400]
    assert "set outside this list" in body, "an unknown model is not offered back"


def test_the_team_count_counts_people_not_tasks():
    """It read `tasks.length` and called it "on the team", so a project with eight
    hired teammates and two open tasks said "2 on the team"."""
    assert "${tasks.length} on the team" not in JS
    assert "lastTeam.length || tasks.length" in JS


def test_idle_teammates_are_visible():
    """The view drew task cards grouped by status, so a teammate only existed once
    they had work — six of eight people on a real run were invisible."""
    assert "function benchHtml(" in JS
    assert "Free right now" in JS
    assert "/team`" in JS, "the roster is never fetched"


def test_a_question_is_the_loudest_thing_in_the_feed():
    """The manager asked something on a live run and it went unnoticed: a faint
    warm tint among a hundred other tinted rows."""
    block = CSS.split(".ev.question {")[1].split("}")[0]
    assert "var(--boss)" in block, "a question should use the colour that means a human is needed"
    assert "border-left: 4px" in block
    assert 'content: "needs you"' in CSS


def test_the_improve_tile_does_not_depend_on_which_screen_you_visited():
    """It was revealed only inside showHome(), so landing straight on a project
    URL left it hidden until something routed you home or you reloaded."""
    assert "function applyPermissions()" in JS
    load_me = JS.split("async function loadMe()")[1][:600]
    assert "applyPermissions()" in load_me
