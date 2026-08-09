"""Thin async git-host REST wrapper.

Every call carries the token it should run under, and callers pass the token of
the PROJECT'S OWNER — never the server's by default. That distinction is the whole
point of this rewrite: the module used to take no token at all and fall through to
config.GITHUB_TOKEN on every request, so a normal user's project opened issues,
pull requests and merges on the OPERATOR'S GitHub identity. A friend who added
their own key saw it ignored while the runs cascaded onto someone else's account.

`token_for(project)` resolves the right one, and for a non-root user with no key
of their own it returns "" rather than the server token — which makes enabled()
false and the operation refuse, instead of silently borrowing the operator's.

The host comes from config, not from a constant. Three shapes are supported:

- **GitHub** — https://api.github.com, the default and the only one that existed
  before. Nothing below changes for it.
- **GitHub Enterprise Server** — https://<host>/api/v3. That is the whole change:
  same API, same request bodies, different address. It gets no code of its own,
  because giving it any would be inventing a difference that is not there.
- **Gitea** — https://<host>/api/v1. Deliberately GitHub-shaped, but not a clone.
  Where it differs, the difference is marked below and was checked against Gitea's
  swagger and source (1.21–1.27), not assumed. Where it is the same call, it is
  left alone: a needless fork is a second code path to keep correct forever.

The failure mode that matters for Gitea is not an error, it is silence — it
ignores query parameters it does not know instead of rejecting them. A GitHub
query sent to Gitea comes back 200 with the wrong rows.
"""

import httpx

from . import config


def _gitea() -> bool:
    return config.GIT_PROVIDER == "gitea"


def auth_headers(token: str = "") -> dict:
    if _gitea():
        # `token` has been accepted by every Gitea release; `Bearer` only since
        # 1.21. The swagger documents `token`, so that is what a self-hoster's
        # reverse proxy and older instance are both known to pass.
        return {"Authorization": f"token {token or config.GITHUB_TOKEN}"}
    return {
        "Authorization": f"Bearer {token or config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# Distinguishes "no token argument was given" (an operator/self-repair caller —
# fall back to the server token) from "a token was resolved and it is empty" (a
# guest with no key of their own — stay disabled, do not borrow the operator's).
# A plain "" default cannot tell those apart, and telling them apart is the fix.
_UNSET = object()


def enabled(repo: str, token=_UNSET) -> bool:
    # A sandbox holds no GitHub token, but reporting "GitHub isn't connected"
    # there is a lie about the CANDIDATE, not about the sandbox: it renders a
    # blocker the real build would not show, hides the PR links a reviewer needs
    # to look at, and makes the copy under test different from the copy shipped.
    if config.DEMO_MODE:
        return bool(repo)
    tok = config.GITHUB_TOKEN if token is _UNSET else token
    return bool(tok and repo)


# ---- human-facing links --------------------------------------------------
#
# Built from GIT_WEB, and never by string-concatenating github.com somewhere in a
# route or a template. Gitea's browser paths differ from GitHub's in two places
# (`/pulls/` not `/pull/`, `/src/branch/` not `/tree/`), so a link that is merely
# host-substituted is a 404 on Gitea.

def repo_url(repo: str) -> str:
    return f"{config.GIT_WEB}/{repo}"


def issue_url(repo: str, number: int) -> str:
    return f"{config.GIT_WEB}/{repo}/issues/{number}"


def pr_url(repo: str, number: int) -> str:
    return f"{config.GIT_WEB}/{repo}/{'pulls' if _gitea() else 'pull'}/{number}"


def branch_url(repo: str, branch: str) -> str:
    seg = "src/branch" if _gitea() else "tree"
    return f"{config.GIT_WEB}/{repo}/{seg}/{branch}"


def clone_url(repo: str, token: str = "") -> str:
    """An HTTPS clone URL carrying the token.

    The `x-access-token` username is GitHub's convention, and Gitea accepts it
    too — its basic-auth path ignores the username whenever a password is present
    and treats the password as the token. So one form covers all three hosts.
    """
    return f"https://x-access-token:{token or config.GITHUB_TOKEN}@" \
           f"{config.GIT_WEB.split('://', 1)[-1]}/{repo}.git"


async def _request(method: str, path: str, token: str = "", **kwargs) -> dict:
    # Single choke point for every call, so the sandbox cannot reach GitHub even
    # if some future code path forgets to check DEMO_MODE.
    if config.DEMO_MODE:
        from . import demo
        return demo.github_call(method, path, kwargs)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(method, f"{config.GIT_API}{path}",
                                    headers=auth_headers(token), **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


def token_for(project: dict) -> str:
    """The GitHub token a project's operations must run under: its owner's.

    A non-root owner with no token of their own gets "" — deliberately, so the
    operation refuses rather than falling through to the operator's credentials.
    An owner-less legacy project (owner_id 0) keeps the server token, which is the
    single-tenant behaviour those projects were created under.
    """
    from . import auth
    owner = auth.get_user((project or {}).get("owner_id") or 0)
    if not owner:
        return config.GITHUB_TOKEN          # legacy single-tenant project
    # get_settings already backfills the server token for root and only for root,
    # so a non-root user's own token (or nothing) is exactly what comes back.
    return auth.get_settings(owner).get("github_token") or ""


async def create_issue(repo: str, title: str, body: str, token: str = "") -> int:
    data = await _request("POST", f"/repos/{repo}/issues", token=token, json={"title": title, "body": body})
    return data["number"]


async def comment_issue(repo: str, number: int, body: str, token: str = "") -> None:
    await _request("POST", f"/repos/{repo}/issues/{number}/comments", token=token, json={"body": body})


async def close_issue(repo: str, number: int, token: str = "") -> None:
    await _request("PATCH", f"/repos/{repo}/issues/{number}", token=token, json={"state": "closed"})


async def create_pr(repo: str, head: str, base: str, title: str, body: str, token: str = "") -> int:
    data = await _request(
        "POST", f"/repos/{repo}/pulls", token=token,
        json={"head": head, "base": base, "title": title, "body": body},
    )
    return data["number"]


async def find_pr_for_branch(repo: str, branch: str, token: str = "") -> int | None:
    """The open PR for this branch, if one already exists.

    A worker with Bash can open its own PR before the scheduler gets there. The
    PR then exists but we never recorded its number, so merge_pr had nothing to
    merge and the UI showed no link. Looking it up makes opening idempotent.
    """
    owner = repo.split("/")[0]
    try:
        if _gitea():
            # Gitea's list-pulls has no `head` filter and ignores the parameter
            # rather than rejecting it, so GitHub's query would return every open
            # PR and we would hand the wrong number to merge_pr. Filter here.
            # (There is a GET /pulls/{base}/{head} endpoint, but it applies no
            # state filter and can hand back a closed PR, which is worse.)
            # `limit` is capped server-side at 50; a branch whose PR falls beyond
            # that page reads as "not found" and we open a second PR — noisy, but
            # not wrong.
            data = await _request("GET", f"/repos/{repo}/pulls?state=open&limit=50", token=token)
            for pr in data or []:
                if (pr.get("head") or {}).get("ref") == branch:
                    return pr["number"]
            return None
        data = await _request(
            "GET", f"/repos/{repo}/pulls?head={owner}:{branch}&state=open&per_page=5",
            token=token)
    except Exception:
        return None
    return data[0]["number"] if data else None


async def merge_pr(repo: str, number: int, token: str = "") -> bool:
    try:
        if _gitea():
            # POST, not PUT, and the method goes in `Do`. Capitalised on purpose:
            # up to 1.25 the Go field carries no json tag, so the decoder matches
            # the field name; 1.26+ accepts either. Getting the key wrong is not
            # an error there — the PR merges with the wrong strategy.
            await _request("POST", f"/repos/{repo}/pulls/{number}/merge", token=token, json={"Do": "squash"})
        else:
            await _request("PUT", f"/repos/{repo}/pulls/{number}/merge", token=token,
                           json={"merge_method": "squash"})
        return True
    except httpx.HTTPStatusError:
        return False


async def default_branch(repo: str, token: str = "") -> str:
    data = await _request("GET", f"/repos/{repo}", token=token)
    return data.get("default_branch", "main")


async def repo_exists(repo: str, token: str = "") -> bool:
    try:
        await _request("GET", f"/repos/{repo}", token=token)
        return True
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return False
        raise


async def list_user_repos(token: str) -> list[dict]:
    """Repos the given token can push to — for the project repo picker."""
    # Gitea paginates with `limit` (hard maximum 50) and supports neither
    # `affiliation` nor `sort`. It also scopes /user/repos to repos the user
    # OWNS, so repos they only collaborate on will not appear in the picker;
    # they can still be typed in by hand. Widening that would mean /repos/search,
    # whose response envelope differs, and I have not verified it against a live
    # instance — better a short list than a wrong one.
    q = "limit=50" if _gitea() else "per_page=100&sort=updated&affiliation=owner,collaborator"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{config.GIT_API}/user/repos?{q}", headers=auth_headers(token))
        resp.raise_for_status()
        return [{"full_name": r["full_name"], "private": r["private"],
                 "updated_at": r.get("updated_at", "")}
                for r in resp.json() if r.get("permissions", {}).get("push")]


async def create_user_repo(token: str, name: str, private: bool = True) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{config.GIT_API}/user/repos",
            headers=auth_headers(token),
            json={"name": name, "private": private, "auto_init": True,
                  "description": "Created by devteam"})
        if resp.status_code >= 300:
            return False, f"{resp.status_code}: {resp.text[:200]}"
        return True, resp.json()["full_name"]


async def list_prs(repo: str, token: str = "") -> list[dict]:
    q = "state=all&limit=30" if _gitea() else "state=all&per_page=30"
    data = await _request("GET", f"/repos/{repo}/pulls?{q}", token=token)
    return [{"number": p["number"], "title": p["title"], "state": p["state"],
             "merged": bool(p.get("merged_at")), "url": p["html_url"]} for p in data]


async def list_branches(repo: str, token: str = "") -> list[str]:
    # `per_page` is not an error on Gitea, it is ignored — which silently gives
    # back the 30-item default page and looks like a repo with fewer branches.
    q = "limit=50" if _gitea() else "per_page=50"
    data = await _request("GET", f"/repos/{repo}/branches?{q}", token=token)
    return [b["name"] for b in data]




async def ensure_repo(repo: str, token: str = "") -> tuple[bool, str]:
    """Create the repo (private, initialized) if it doesn't exist.
    Returns (ok, note). `repo` is 'owner/name'; the token's user must be the owner
    (or the org must allow it). Personal repos are created for the token's own user."""
    if not enabled(repo, token):
        return False, "GitHub not configured"
    if await repo_exists(repo, token):
        return True, "repo already exists"
    owner, _, name = repo.partition("/")
    me = await _request("GET", "/user", token=token)
    body = {"name": name, "private": True, "auto_init": True,
            "description": "Created automatically by devteam"}
    try:
        if owner and owner.lower() != me.get("login", "").lower():
            # owner is an org the token can create in
            await _request("POST", f"/orgs/{owner}/repos", token=token, json=body)
        else:
            await _request("POST", "/user/repos", token=token, json=body)
        return True, f"created private repo {repo}"
    except httpx.HTTPStatusError as e:
        return False, f"could not create {repo}: {e.response.status_code} {e.response.text[:200]}"


async def list_tree(repo: str, ref: str = "", token: str = "") -> list[dict]:
    """Every file in the repo at `ref`, flat. The repo is the source of truth for
    what the team produced — a local checkout is a copy that can be stale."""
    if not ref:
        ref = await default_branch(repo, token)
    skip = (".git/", "node_modules/", "__pycache__/", ".venv/", "dist/", "build/")
    entries: list[dict] = []
    page = 1
    while True:
        # Gitea caps this at 1000 entries a page and sets `truncated` to mean
        # "there is another page". GitHub returns the whole tree, and its
        # `truncated` means it gave up — paging there would be a second identical
        # request, so only Gitea loops.
        q = f"?recursive=1&page={page}&per_page=1000" if _gitea() else "?recursive=1"
        data = await _request("GET", f"/repos/{repo}/git/trees/{ref}{q}", token=token)
        entries += data.get("tree", [])
        if not (_gitea() and data.get("truncated")):
            break
        page += 1
    return [{"path": t["path"], "size": t.get("size", 0)}
            for t in entries
            if t.get("type") == "blob" and not t["path"].startswith(skip)]


async def read_file(repo: str, path: str, ref: str = "", token: str = "") -> str:
    """One file's contents, decoded. Binary and oversized files are refused rather
    than returned as mojibake."""
    import base64
    q = f"?ref={ref}" if ref else ""
    data = await _request("GET", f"/repos/{repo}/contents/{path}{q}", token=token)
    if data.get("encoding") != "base64" or data.get("size", 0) > 400_000:
        raise ValueError("too large or not a text file")
    raw = base64.b64decode(data["content"])
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("not a text file")
