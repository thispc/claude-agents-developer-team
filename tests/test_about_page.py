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
