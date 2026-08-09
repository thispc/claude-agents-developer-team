"""The gate that stops a feature shipping without anyone writing it down.

Documentation drifts silently: nothing breaks and nothing warns. This handbook
had three separate false claims within days of being written — three autonomy
modes where two exist, containerised workers that are actually processes, and a
line count wrong by more than double. All three were written in good faith.

So the facts are asserted against their source, and the capability list is a gate
in front of shipping: add a manager tool and the suite fails until it is either
documented or explicitly marked as plumbing.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.hostonly     # reads the repo, not the running image

ROOT = Path(__file__).resolve().parent.parent


# Read inside the tests, not at import.
#
# `hostonly` deselects these when the suite runs inside the image, but a marker
# cannot save a module that fails to IMPORT — collection happens first, and
# docs/ is not shipped in the image. Reading at module level took the whole run
# down with a FileNotFoundError instead of quietly skipping 11 tests.
def _read(*parts) -> str:
    return (ROOT.joinpath(*parts)).read_text()


class _Lazy:
    """Reads on first use, so `HANDBOOK in ...` still looks like a string to every
    test below while costing nothing at import time."""

    def __init__(self, *parts):
        self._parts = parts
        self._text = None

    def _load(self) -> str:
        if self._text is None:
            self._text = _read(*self._parts)
        return self._text

    def __contains__(self, other): return other in self._load()
    def __str__(self): return self._load()
    def lower(self): return self._load().lower()
    def split(self, *a, **k): return self._load().split(*a, **k)


HANDBOOK = _Lazy("docs", "HANDBOOK.md")
ABOUT = _Lazy("dashboard", "index.html")


# --- the gate --------------------------------------------------------------

def test_every_capability_is_findable_in_the_handbook():
    """The list is the contract. If a capability is not in the handbook, either
    the page is stale or the entry is wrong — both need a human, not a nudge."""
    from app import capabilities
    missing = [f"{k} (looked for {v['doc_phrase']!r})"
               for k, v in capabilities.CAPABILITIES.items()
               if v["doc_phrase"].lower() not in str(HANDBOOK).lower()]
    assert not missing, "these capabilities are not documented:\n  " + "\n  ".join(missing)


def test_no_manager_tool_ships_without_being_accounted_for():
    """A manager tool is the closest thing this system has to a user-facing
    feature: it is a new thing the platform will do on its own. Adding one now
    fails here until it is either mapped to a documented capability or written
    down as plumbing."""
    from app import capabilities, manager
    tools = {name.replace("mcp__team__", "") for name in manager.MANAGER_TOOLS}
    unaccounted = tools - capabilities.UNDOCUMENTED_TOOLS
    assert not unaccounted, (
        f"new manager tool(s) {sorted(unaccounted)} — document the capability in "
        f"capabilities.CAPABILITIES and the handbook, or list it in "
        f"UNDOCUMENTED_TOOLS with a reason")


def test_the_plumbing_list_does_not_rot():
    """An entry naming a tool that no longer exists means the list has stopped
    describing anything, and a stale allow-list quietly lets real features through."""
    from app import capabilities, manager
    tools = {name.replace("mcp__team__", "") for name in manager.MANAGER_TOOLS}
    stale = capabilities.UNDOCUMENTED_TOOLS - tools
    assert not stale, f"UNDOCUMENTED_TOOLS names tools that no longer exist: {sorted(stale)}"


# --- claims that were wrong before -----------------------------------------

def _real_modes() -> set[str]:
    from app import db
    comment = re.search(r"autonomy TEXT NOT NULL DEFAULT '\w+',\s*--\s*([\w |]+)",
                        db.SCHEMA).group(1)
    return {m.strip() for m in comment.split("|") if m.strip()}


def test_the_documented_autonomy_modes_are_the_ones_that_exist():
    """The page listed three — "ask me", "supervised", "autonomous". There are
    two: the first two were the same mode described twice, which makes the whole
    table read as though someone had guessed at it.

    Parsed from the tables rather than searched for as text, because "Ask me
    first" is a perfectly good BUTTON label for supervised mode. The bug was a
    third row in a list of modes, not the phrase appearing anywhere.
    """
    real = _real_modes()
    assert real == {"supervised", "autonomous"}, real

    # The handbook's table, taken from the section that introduces the modes.
    section = str(HANDBOOK).split("how much you are involved")[1].split("\n\n##")[0]
    rows = re.findall(r"^\|\s*\*?\*?([A-Za-z ]+?)\*?\*?\s*(?:\*\(default\)\*)?\s*\|",
                      section, re.M)
    documented = {r.strip().lower() for r in rows} - {"mode", "---"}
    assert documented == real, f"handbook documents {documented}, code has {real}"

    # The About page's table, marked with data-doc so it can be found reliably.
    table = str(ABOUT).split('data-doc="autonomy-modes"')[1].split("</table>")[0]
    labels = {m.lower() for m in re.findall(r"<td><b>([A-Za-z ]+)</b>", table)}
    assert labels == real, f"About page documents {labels}, code has {real}"


def test_the_two_places_that_document_modes_agree_with_each_other():
    """Two copies of the same table is two chances to be wrong. They must at
    least be wrong together."""
    assert ("Supervised" in str(HANDBOOK)) == ("Supervised" in str(ABOUT))
    assert ("Autonomous" in str(HANDBOOK)) == ("Autonomous" in str(ABOUT))


def test_contests_are_documented_as_optional_because_they_are_off_by_default():
    """A reader who thinks every task runs three ways will expect a bill that
    never arrives."""
    from app import db
    assert "compete INTEGER NOT NULL DEFAULT 0" in db.SCHEMA
    assert "used sparingly" in str(HANDBOOK).lower() or "optional" in str(HANDBOOK).lower()


def test_the_escalation_ladder_in_the_docs_is_the_real_one():
    from app import launcher
    assert launcher.FALLBACK_ORDER == [
        "claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"]
    assert "Cheap first" in HANDBOOK


def test_no_claim_that_workers_are_containerised():
    """LAUNCHER=local is what runs, so workers are subprocesses sharing the
    conductor's container. Claiming otherwise reads as a security guarantee."""
    for doc in (str(HANDBOOK), str(ABOUT)):
        assert "boxed into an isolated container" not in doc
        assert "separate" in doc and "process" in doc


def test_this_module_imports_without_the_docs_present():
    """`hostonly` deselects these inside the image, but a marker cannot save a
    module that fails to IMPORT — collection happens first. Reading docs/ at
    module level took the whole in-pod run down with a FileNotFoundError instead
    of quietly skipping eleven tests, which is how a green gate becomes a red one
    for a reason that has nothing to do with the build."""
    import ast
    src = (Path(__file__)).read_text()
    module_level = [ast.unparse(n) for n in ast.parse(src).body
                    if isinstance(n, ast.Assign) and "read_text()" in ast.unparse(n)]
    assert not module_level, f"these run at import time: {module_level}"
