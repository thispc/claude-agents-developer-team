"""A guest's project must never touch the operator's credentials.

The reported incident: a friend added his own GitHub token, the operator's was
already configured as root, and the runs cascaded onto the operator's account.
The cause was that github_client took no token at all — every API call fell
through to config.GITHUB_TOKEN — so a normal user's project opened issues, pull
requests and merges as the operator, and the friend's key was ignored.

These tests are the guard rail. They assert the property directly (a non-root
owner never yields the server token) and structurally (no project-operating call
site resolves a token any way but through the owner), so the same leak cannot be
reintroduced by a future call site that forgets.
"""

import re
from pathlib import Path

import pytest

from conftest import make_project
from app import auth, config, db, github_client

APP = Path(__file__).resolve().parent.parent / "conductor" / "app"


def _user(name, token=None, is_root=False):
    uid = auth.create_user(name, "pw-" + name, is_root=is_root)
    if token is not None:
        auth.save_settings(uid, {"github_token": token})
    return uid


# --- the property ----------------------------------------------------------

def test_a_non_root_users_project_uses_that_users_token(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "OPERATOR-SECRET")
    uid = _user("friend", token="FRIEND-TOKEN")
    p = db.get_project(make_project(owner_id=uid))
    assert github_client.token_for(p) == "FRIEND-TOKEN"


def test_a_non_root_user_with_no_token_does_not_borrow_the_operators(fresh_db, monkeypatch):
    """The exact leak. A guest with no key of their own must get nothing — so the
    operation refuses — never the operator's token."""
    monkeypatch.setattr(config, "GITHUB_TOKEN", "OPERATOR-SECRET")
    uid = _user("guest", token=None)
    p = db.get_project(make_project(owner_id=uid))
    assert github_client.token_for(p) == ""
    # And because it is empty, GitHub reads as not-connected rather than connected
    # on someone else's credentials.
    assert github_client.enabled(p["repo"] or "o/r", github_client.token_for(p)) is False


def test_root_still_uses_the_operator_credentials(fresh_db, monkeypatch):
    """Isolation must not break the single-operator case, which is most setups."""
    monkeypatch.setattr(config, "GITHUB_TOKEN", "OPERATOR-SECRET")
    root = auth.get_user_by_name("root")
    p = db.get_project(make_project(owner_id=root["id"]))
    assert github_client.token_for(p) == "OPERATOR-SECRET"


def test_a_legacy_ownerless_project_keeps_the_server_token(fresh_db, monkeypatch):
    """Projects created before multi-user existed have owner_id 0 and ran on the
    server token; changing that would break them for a security property they
    never violated."""
    monkeypatch.setattr(config, "GITHUB_TOKEN", "OPERATOR-SECRET")
    p = db.get_project(make_project(owner_id=0))
    assert github_client.token_for(p) == "OPERATOR-SECRET"


def test_the_worker_clone_token_is_the_owners_too(fresh_db, monkeypatch):
    """A worker clones and pushes; doing that as the operator is the same leak as
    opening a PR as the operator."""
    from app import launcher
    monkeypatch.setattr(config, "GITHUB_TOKEN", "OPERATOR-SECRET")
    uid = _user("friend2", token="FRIEND-TOKEN")
    p = db.get_project(make_project(owner_id=uid))
    assert launcher.owner_github_token(p) == "FRIEND-TOKEN"

    guest = _user("guest2", token=None)
    pg = db.get_project(make_project(owner_id=guest))
    assert launcher.owner_github_token(pg) == ""


def test_a_guest_project_does_not_default_to_the_operators_repo(fresh_db, monkeypatch,
                                                                make_user):
    """The other half of the cascade: a user who left the repo field blank had
    their project pointed at the operator's default repository."""
    monkeypatch.setattr(config, "GITHUB_REPO", "operator/private-repo")
    uid, client = make_user("norah")
    auth.save_settings(uid, {"github_token": "NORAH-TOKEN",
                             "anthropic_api_key": "sk-ant-x"})
    r = client.post("/api/projects", json={"name": "hers", "brief": "b"})
    # It must not silently adopt operator/private-repo. Either it errors for a
    # missing repo or stores an empty one — both are fine; borrowing is not.
    if r.status_code == 200:
        assert db.get_project(r.json()["id"])["repo"] != "operator/private-repo"


# --- the structural guard --------------------------------------------------

def test_the_provider_key_path_never_reads_server_credentials(fresh_db):
    """The AI side was already isolated — key_for reads only from the user's
    settings — and it must stay that way, or the round table and planner would
    start spending the operator's quota."""
    src = (APP / "providers.py").read_text()
    body = src.split("def key_for(")[1].split("\ndef ")[0]
    for leak in ("config.ANTHROPIC_API_KEY", "config.OPENAI_API_KEY",
                 "config.GEMINI_API_KEY", "config.CLAUDE_CODE_OAUTH_TOKEN"):
        assert leak not in body, f"key_for reaches for {leak} — that is the operator's"


def test_no_project_clone_uses_the_bare_server_token(fresh_db):
    """clone_url(repo, config.GITHUB_TOKEN) is the fingerprint of the old leak:
    fetching a user's repo with the operator's credentials. Every clone must pass
    a resolved owner token instead."""
    for mod in ("deploy.py", "preview.py"):
        src = (APP / mod).read_text()
        assert "config.GITHUB_TOKEN" not in src, (
            f"{mod} still reaches for the server token directly — resolve the "
            f"owner's with github_client.token_for(project)")


def test_the_manager_threads_a_token_into_every_github_call(fresh_db):
    """The manager operates entirely on its project's repo. A call that omits the
    token falls through to the server's, which is the leak — so none may."""
    src = (APP / "manager.py").read_text()
    # Every github_client call that hits the network takes a token now.
    for call in ("create_issue(", "comment_issue(", "close_issue(", "merge_pr(",
                 "ensure_repo("):
        for m in re.finditer(re.escape("github_client." + call), src):
            window = src[m.start():m.start() + 400]
            assert "token=" in window or "token_for" in window, (
                f"github_client.{call} in manager.py does not pass a token")
