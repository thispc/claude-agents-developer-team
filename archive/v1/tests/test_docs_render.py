"""Rendering a document an agent wrote, without handing it the operator's session.

Deliverables from non-code projects are markdown, and the dashboard used to dump
them as plain text. Rendering them means the dashboard now interprets bytes that
came out of a repository nobody here controls — so most of what follows is not
about headings, it is about what an agent can put in a file to attack the person
reading it.

Two layers, deliberately:

- The behaviour tests run the renderer for real under node. node is not part of
  the deployed pod (there is no build step and no npm), so they SKIP where it is
  absent — a green run on a box without node has not tested the renderer.
- The source tests below them always run. They pin the invariants that make the
  renderer safe at all, so removing escapeHtml or reintroducing an innerHTML of
  raw file text fails everywhere, node or no node.
"""

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from conftest import dashboard_js  # the split dashboard JS, concatenated in load order

DASH = Path(__file__).resolve().parent.parent / "dashboard"

MARK_START = "// >>> markdown renderer"
MARK_END = "// <<< markdown renderer"

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="no node available to execute app.js")


def _renderer_source() -> str:
    """The renderer, cut out of app.js and stood up on its own.

    The alternative — a headless browser, or a JS test runner — means a toolchain
    the pod cannot install. Cutting on the markers keeps the tested code and the
    shipped code the same bytes.
    """
    src = dashboard_js()
    body = src.split(MARK_START, 1)[1].split("\n", 1)[1].split(MARK_END, 1)[0]
    esc = re.search(r"function escapeHtml\(s\) \{.*?\n\}", src, re.S)
    assert esc, "escapeHtml is gone; the renderer has nothing to escape with"
    return esc.group(0) + "\n" + body


def render(md: str) -> str:
    driver = _renderer_source() + textwrap.dedent("""
        let buf = "";
        process.stdin.on("data", (c) => { buf += c; });
        process.stdin.on("end", () => process.stdout.write(renderMarkdown(JSON.parse(buf))));
    """)
    p = subprocess.run(["node", "-e", driver], input=json.dumps(md),
                       capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr
    return p.stdout


# ---- the attacks -----------------------------------------------------------
#
# Each of these is something an agent can legally write into a markdown file in a
# repository the team cloned. The operator opens the deliverable in a session that
# can start deployments and read every credential the dashboard can read.

@node
def test_raw_html_is_shown_not_executed():
    out = render("Hello <script>alert(1)</script> there")
    assert "<script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


@node
def test_an_image_tag_with_an_event_handler_is_inert():
    out = render('<img src=x onerror="alert(1)">')
    assert "<img" not in out
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in out


@node
def test_javascript_urls_are_refused():
    out = render("[click me](javascript:alert(document.cookie))")
    assert "javascript:" not in out
    assert "md-blocked" in out and "click me" in out


def hrefs(out: str) -> list[str]:
    return re.findall(r'<a href="([^"]*)"', out)


@node
@pytest.mark.parametrize("url", [
    "JaVaScRiPt:alert(1)",          # scheme matching is case-insensitive in browsers
    "java\tscript:alert(1)",        # embedded tab: browsers strip it, a naive check does not
    " javascript:alert(1)",         # leading space
    "vbscript:msgbox(1)",           # the forgotten one every blocklist misses
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "//evil.example/x",             # protocol-relative: inherits ours and leaves the host
    "file:///etc/passwd",
])
def test_no_href_ever_carries_a_scheme_we_did_not_allow(url):
    """The assertion is on the href, not on whether a link appeared: "java\\tscript:"
    legally ends at the tab, so it renders as the relative target "java" — a dead
    link, which is the correct outcome, and not a scheme."""
    for href in hrefs(render(f"[x]({url})")):
        assert not re.match(r"[^/?#]*:", href) or re.match(r"(?i)(https?|mailto):", href), href


@node
@pytest.mark.parametrize("url", [
    "https://example.com/a?b=1",
    "http://example.com",
    "mailto:someone@example.com",
    "./other-doc.md",
    "docs/design.md",
    "#a-heading-in-this-file",
])
def test_ordinary_links_still_work(url):
    out = render(f"[x]({url})")
    assert f'href="{url}"' in out
    assert 'rel="noopener noreferrer nofollow"' in out, "target=_blank without noopener"


@node
def test_a_link_cannot_break_out_of_the_href_attribute():
    """The quote that would close the attribute is already &quot; by the time the
    URL is looked at, so the handler stays inside the value instead of becoming
    one. The link is dead; nothing runs."""
    out = render('[x](https://a.example/" onmouseover="alert(1))')
    tag = out.split(">", 1)[0] if "<a " not in out else re.search(r"<a [^>]*>", out).group(0)
    assert "onmouseover=" not in tag
    assert hrefs(out) == ["https://a.example/&quot;"]


@node
def test_a_link_title_cannot_smuggle_attributes():
    out = render('[x](https://a.example "t) autofocus onfocus=alert(1)")')
    assert "onfocus" not in out.split("</a>")[0]


@node
def test_image_syntax_does_not_fetch_a_remote_asset():
    """A remote <img> named by an untrusted document leaks the operator's address
    to whoever wrote the repo, and makes the dashboard fetch on their behalf."""
    out = render("![alt text](https://tracker.example/pixel.png)")
    assert "<img" not in out
    assert "alt text" in out and "tracker.example" in out


@node
def test_a_fence_language_cannot_close_the_class_attribute():
    out = render('```js" onload="alert(1)\nx = 1\n```')
    assert "onload" not in out or "&quot;" in out
    assert re.search(r'class="md-code[^"]*"', out)
    assert "<script" not in out


@node
@pytest.mark.parametrize("lang", ["constructor", "__proto__", "toString"])
def test_a_fence_language_cannot_reach_through_a_prototype(lang):
    """The info string is the document's to choose, so it is a lookup key an
    attacker controls — on a plain object those three all answer truthy."""
    out = render(f"```{lang}\nx\n```")
    assert "figcaption" not in out
    assert "native code" not in out


@node
def test_table_cells_are_escaped_like_everything_else():
    out = render("| a | b |\n| --- | --- |\n| <img src=x onerror=alert(1)> | ok |")
    assert "<img" not in out
    assert "<table" in out and "<td>" in out


@node
def test_headings_and_list_items_are_escaped_too():
    out = render("# <b>bold</b>\n\n- <iframe src=javascript:alert(1)>")
    assert "<b>bold</b>" not in out and "&lt;b&gt;bold&lt;/b&gt;" in out
    assert "<iframe" not in out


@node
def test_html_inside_a_code_span_or_fence_stays_text():
    out = render("Use `<script>` here\n\n```\n<script>alert(1)</script>\n```")
    assert "<script>" not in out
    assert out.count("&lt;script&gt;") >= 2


@node
def test_deep_nesting_does_not_blow_the_stack():
    """20k nested quotes is four bytes per level to write and a hung tab to read."""
    out = render("> " * 20000 + "boom")
    assert "boom" in out


# ---- the parts that are actually about documents ---------------------------

@node
def test_the_ordinary_shapes_render():
    out = render(textwrap.dedent("""\
        # Title

        Some **bold** and *italic* and `code`.

        - one
        - two

        1. first
        2. second

        > a quote

        | h1 | h2 |
        | --- | --- |
        | a | b |

        ---

        ```python
        x = 1
        ```
    """))
    for frag in ("<h1>Title</h1>", "<strong>bold</strong>", "<em>italic</em>",
                 "<code>code</code>", "<ul>", "<ol>", "<blockquote>", "<table",
                 "<hr>", "lang-python"):
        assert frag in out, f"missing {frag}"


@node
def test_a_mermaid_block_says_it_is_not_drawn_instead_of_pretending():
    out = render("```mermaid\ngraph TD; A-->B;\n```")
    assert "not drawn" in out
    assert "graph TD; A--&gt;B;" in out
    assert "<svg" not in out


# ---- invariants that hold with or without node ------------------------------

def test_only_markdown_files_reach_innerhtml_and_only_via_the_renderer():
    """The one place repository content becomes markup. If this ever grows a second
    innerHTML of file text, the escaping above stops being the whole story."""
    block = dashboard_js().split("async function openProjectFile(", 1)[1].split("\n}", 1)[0]
    for assignment in re.findall(r"innerHTML\s*=\s*(.+)", block):
        assert assignment.startswith("renderMarkdown("), assignment
    assert "textContent = d.text" in block, "non-markdown files must stay text"


def test_the_renderer_escapes_and_the_markers_are_intact():
    src = dashboard_js()
    assert MARK_START in src and MARK_END in src, "the node tests extract on these"
    body = src.split(MARK_START, 1)[1].split(MARK_END, 1)[0]
    assert "escapeHtml(" in body
    assert "mdSafeUrl(" in body


def test_urls_are_allowlisted_not_blocklisted():
    """A blocklist of javascript:/vbscript:/data: is a list of the tricks we thought
    of. Anything that is not plainly a document link has to be refused instead."""
    src = dashboard_js()
    body = src.split(MARK_START, 1)[1].split(MARK_END, 1)[0]
    assert re.search(r"MD_SAFE_SCHEME\s*=\s*/\^\(\?:https\?:\|mailto:\)/i", body)


def test_code_files_are_not_run_through_the_markdown_renderer():
    """Reflowing a source file, or turning its '#' comments into headings, makes it
    unreadable — and would render its string literals as markup."""
    src = dashboard_js()
    assert re.search(r"MD_EXT\s*=\s*/\\\.\(md\|markdown\)\$/i", src)
    css = (DASH / "style.css").read_text()
    assert ".file-md" in css and "white-space: normal" in css


def test_no_diagram_library_is_loaded_from_anywhere():
    """No build step, no npm, no CDN: a <script src> would simply fail to load in
    the pod, and silently."""
    html = (DASH / "index.html").read_text()
    assert "mermaid" not in html.lower()
    assert not re.search(r'<script[^>]+src="https?://', html)
