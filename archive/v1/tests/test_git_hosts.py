"""Git means "a git host", not github.com.

The API host used to be a constant in github_client, so "bring your own git" was
the one promise the platform could not keep: an in-house GitHub Enterprise or
Gitea server was unusable. These tests hold two lines at once — that the host is
now configuration, and that configuring nothing leaves an existing GitHub install
sending byte-for-byte the same requests it sent before.
"""

import httpx
import pytest

from app import config, github_client


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.content = b"{}"

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture()
def sent(monkeypatch):
    """Record every outbound request instead of making it.

    Patched at the httpx layer rather than at github_client's, because the thing
    under test IS the URL that reaches the wire.
    """
    class _Calls(list):
        pass

    calls = _Calls()
    replies: list = []

    async def _request(self, method, url, **kw):
        calls.append({"method": method, "url": str(url), "json": kw.get("json"),
                      "headers": kw.get("headers") or {}})
        return _Resp(replies.pop(0) if replies else {})

    async def _get(self, url, **kw):
        return await _request(self, "GET", url, **kw)

    async def _post(self, url, **kw):
        return await _request(self, "POST", url, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "request", _request)
    monkeypatch.setattr(httpx.AsyncClient, "get", _get)
    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    calls.reply_with = replies.extend        # type: ignore[attr-defined]
    return calls


@pytest.fixture()
def gitea(monkeypatch):
    monkeypatch.setattr(config, "GIT_PROVIDER", "gitea")
    monkeypatch.setattr(config, "GIT_API", "https://git.example.com/api/v1")
    monkeypatch.setattr(config, "GIT_WEB", "https://git.example.com")


# ---- the default must not have moved -------------------------------------

def test_configuring_nothing_still_means_github():
    assert config.GIT_API == "https://api.github.com"
    assert config.GIT_WEB == "https://github.com"
    assert config.GIT_PROVIDER == "github"


async def test_a_default_install_sends_exactly_the_request_it_always_sent(sent):
    sent.reply_with([{"number": 12}])
    await github_client.create_issue("o/r", "a title", "a body")
    call = sent[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.github.com/repos/o/r/issues"
    assert call["json"] == {"title": "a title", "body": "a body"}
    assert call["headers"] == {
        "Authorization": "Bearer ",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def test_github_merges_with_a_put_and_merge_method(sent):
    await github_client.merge_pr("o/r", 4)
    assert sent[0]["method"] == "PUT"
    assert sent[0]["url"] == "https://api.github.com/repos/o/r/pulls/4/merge"
    assert sent[0]["json"] == {"merge_method": "squash"}


async def test_github_still_asks_for_a_branch_s_pr_by_head(sent):
    sent.reply_with([[{"number": 9}]])
    assert await github_client.find_pr_for_branch("o/r", "task/3") == 9
    assert sent[0]["url"] == (
        "https://api.github.com/repos/o/r/pulls?head=o:task/3&state=open&per_page=5")


def test_the_clone_url_is_the_one_git_has_always_been_given():
    assert github_client.clone_url("o/r", "tok") == \
        "https://x-access-token:tok@github.com/o/r.git"


# ---- GitHub Enterprise Server is a base URL and nothing else -------------

async def test_an_enterprise_base_url_is_used_verbatim(sent, monkeypatch):
    """GHES serves GitHub's own API under /api/v3. Anything beyond taking the
    configured prefix as given would be inventing a difference."""
    monkeypatch.setattr(config, "GIT_API", "https://git.corp.example/api/v3")
    sent.reply_with([{"number": 3}])
    await github_client.create_issue("o/r", "t", "b")
    assert sent[0]["url"] == "https://git.corp.example/api/v3/repos/o/r/issues"


async def test_enterprise_is_not_treated_as_a_different_provider(sent, monkeypatch):
    monkeypatch.setattr(config, "GIT_API", "https://git.corp.example/api/v3")
    await github_client.merge_pr("o/r", 1)
    assert sent[0]["method"] == "PUT" and sent[0]["json"] == {"merge_method": "squash"}


def test_enterprise_links_point_at_its_own_web_host(monkeypatch):
    monkeypatch.setattr(config, "GIT_WEB", "https://git.corp.example")
    assert github_client.pr_url("o/r", 7) == "https://git.corp.example/o/r/pull/7"
    assert github_client.clone_url("o/r", "tok") == \
        "https://x-access-token:tok@git.corp.example/o/r.git"


# ---- Gitea: only the differences that are real ---------------------------

async def test_gitea_authenticates_with_the_token_scheme(sent, gitea):
    await github_client.comment_issue("o/r", 1, "hi")
    assert sent[0]["headers"] == {"Authorization": "token "}


async def test_gitea_merges_with_a_post_and_a_capitalised_do(sent, gitea):
    """A wrong key is not an error on Gitea — the PR merges with the default
    strategy instead, which is the kind of failure nobody notices."""
    await github_client.merge_pr("o/r", 4)
    assert sent[0]["method"] == "POST"
    assert sent[0]["url"] == "https://git.example.com/api/v1/repos/o/r/pulls/4/merge"
    assert sent[0]["json"] == {"Do": "squash"}


async def test_gitea_filters_prs_by_branch_itself(sent, gitea):
    """Gitea has no `head` filter and ignores the parameter rather than rejecting
    it, so GitHub's query would hand back every open PR and we would merge the
    wrong one."""
    sent.reply_with([[{"number": 2, "head": {"ref": "task/1"}},
                      {"number": 5, "head": {"ref": "task/3"}}]])
    assert await github_client.find_pr_for_branch("o/r", "task/3") == 5
    assert "head=" not in sent[0]["url"]


async def test_gitea_reports_no_pr_when_no_branch_matches(sent, gitea):
    sent.reply_with([[{"number": 2, "head": {"ref": "other"}}]])
    assert await github_client.find_pr_for_branch("o/r", "task/3") is None


async def test_gitea_paginates_with_limit(sent, gitea):
    """`per_page` is not rejected there, it is ignored — which silently returns
    the 30-item default page and reads as a repo with fewer branches."""
    sent.reply_with([[{"name": "main"}]])
    await github_client.list_branches("o/r")
    assert sent[0]["url"].endswith("/repos/o/r/branches?limit=50")


async def test_gitea_keeps_reading_the_tree_while_it_says_truncated(sent, gitea):
    """Gitea pages the tree at 1000 entries and uses `truncated` to mean "there is
    another page". Stopping at the first page loses most of a real repo."""
    sent.reply_with([
        {"tree": [{"path": "a.py", "type": "blob"}], "truncated": True},
        {"tree": [{"path": "b.py", "type": "blob"}], "truncated": False},
    ])
    files = await github_client.list_tree("o/r", "main")
    assert [f["path"] for f in files] == ["a.py", "b.py"]
    assert "page=2" in sent[1]["url"]


async def test_github_reads_the_tree_in_one_request(sent):
    """GitHub's `truncated` means it gave up, not that a next page exists; asking
    again would be the same request twice."""
    sent.reply_with([{"tree": [{"path": "a.py", "type": "blob"}], "truncated": True}])
    await github_client.list_tree("o/r", "main")
    assert len(sent) == 1
    assert sent[0]["url"].endswith("/repos/o/r/git/trees/main?recursive=1")


async def test_gitea_reads_a_file_the_same_way(sent, gitea):
    """Contents is one of the endpoints Gitea did not change, so neither do we."""
    import base64
    sent.reply_with([{"encoding": "base64", "size": 5,
                      "content": base64.b64encode(b"hello").decode()}])
    assert await github_client.read_file("o/r", "a.txt") == "hello"
    assert sent[0]["url"] == "https://git.example.com/api/v1/repos/o/r/contents/a.txt"


def test_gitea_links_use_gitea_s_own_browser_paths(gitea):
    """Host substitution alone is a 404: Gitea serves /pulls/ and /src/branch/."""
    assert github_client.pr_url("o/r", 7) == "https://git.example.com/o/r/pulls/7"
    assert github_client.branch_url("o/r", "task/3") == \
        "https://git.example.com/o/r/src/branch/task/3"
    assert github_client.issue_url("o/r", 7) == "https://git.example.com/o/r/issues/7"
    assert github_client.repo_url("o/r") == "https://git.example.com/o/r"


# ---- the remote parser --------------------------------------------------

def test_the_self_repo_is_read_off_any_host(monkeypatch):
    """It used to split the remote on the literal "github.com" and hand back the
    whole URL as the repo name when the host was anything else."""
    from app import selfops

    class _R:
        def __init__(self, out):
            self.stdout = out

    for url, want in (
        ("https://github.com/acme/devteam.git", "acme/devteam"),
        ("git@github.com:acme/devteam.git", "acme/devteam"),
        ("https://git.example.com/acme/devteam.git", "acme/devteam"),
        ("ssh://git@git.example.com:2222/acme/devteam.git", "acme/devteam"),
    ):
        monkeypatch.setattr(selfops, "_sh", lambda *a, _o=url, **k: _R(_o))
        monkeypatch.setattr(config, "SELF_REPO", "")
        assert selfops.self_repo() == want
