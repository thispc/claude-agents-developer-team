"""The About page, and the one colour rule that carries meaning.

Both are source-level assertions rather than browser tests, because the suite has
no browser. That limit is worth stating: these prove the page is wired and the
palette is consistent, not that either looks right.
"""

from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent.parent / "dashboard"
HTML = (DASH / "index.html").read_text()
JS = (DASH / "app.js").read_text()
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
    assert '#/about' in JS.split("function route()")[1][:600]


def test_every_screen_hides_the_about_page():
    """A screen left visible underneath another is the classic single-page bug:
    it does not error, it just renders two pages on top of each other."""
    for opener in ("function showHome", "async function openSelfRepair",
                   "function openProject", "async function openPlan"):
        body = JS.split(opener)[1][:700]
        assert "aboutPage" in body, f"{opener} does not hide the About page"


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
    routes = (Path(__file__).resolve().parent.parent
              / "conductor" / "app" / "routes.py").read_text()
    actual = routes.count("@router.")
    assert f"There are {actual}" in _handbook() or f"are {actual};" in _handbook(), \
        f"the handbook does not say {actual} endpoints"


def test_the_internal_door_really_has_three_requests_behind_it():
    """The page tells a reader that a teammate can do exactly three things. If a
    fourth is added, that sentence becomes a false security claim."""
    routes = (Path(__file__).resolve().parent.parent
              / "conductor" / "app" / "routes.py").read_text()
    internal = routes.count('@router.get("/internal/') + routes.count('@router.post("/internal/')
    assert internal == 3, (
        f"{internal} internal endpoints exist; the About page and handbook both say "
        "three, and both need updating")


def test_the_table_count_matches_the_schema():
    from app import db, auth, findings
    declared = sum(mod.SCHEMA.count("CREATE TABLE IF NOT EXISTS")
                   for mod in (db, auth, findings))
    assert "Eighteen tables" in _handbook() and declared == 18, \
        f"{declared} tables are declared; the handbook says eighteen"


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
