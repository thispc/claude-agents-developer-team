"""Thin async GitHub REST wrapper. Only the conductor holds the token for API calls."""

import httpx

from . import config

API = "https://api.github.com"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def enabled(repo: str) -> bool:
    # A sandbox holds no GitHub token, but reporting "GitHub isn't connected"
    # there is a lie about the CANDIDATE, not about the sandbox: it renders a
    # blocker the real build would not show, hides the PR links a reviewer needs
    # to look at, and makes the copy under test different from the copy shipped.
    if config.DEMO_MODE:
        return bool(repo)
    return bool(config.GITHUB_TOKEN and repo)


async def _request(method: str, path: str, **kwargs) -> dict:
    # Single choke point for every call, so the sandbox cannot reach GitHub even
    # if some future code path forgets to check DEMO_MODE.
    if config.DEMO_MODE:
        from . import demo
        return demo.github_call(method, path, kwargs)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(method, f"{API}{path}", headers=_headers(), **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


async def create_issue(repo: str, title: str, body: str) -> int:
    data = await _request("POST", f"/repos/{repo}/issues", json={"title": title, "body": body})
    return data["number"]


async def comment_issue(repo: str, number: int, body: str) -> None:
    await _request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})


async def close_issue(repo: str, number: int) -> None:
    await _request("PATCH", f"/repos/{repo}/issues/{number}", json={"state": "closed"})


async def create_pr(repo: str, head: str, base: str, title: str, body: str) -> int:
    data = await _request(
        "POST", f"/repos/{repo}/pulls",
        json={"head": head, "base": base, "title": title, "body": body},
    )
    return data["number"]


async def find_pr_for_branch(repo: str, branch: str) -> int | None:
    """The open PR for this branch, if one already exists.

    A worker with Bash can open its own PR before the scheduler gets there. The
    PR then exists but we never recorded its number, so merge_pr had nothing to
    merge and the UI showed no link. Looking it up makes opening idempotent.
    """
    owner = repo.split("/")[0]
    try:
        data = await _request(
            "GET", f"/repos/{repo}/pulls?head={owner}:{branch}&state=open&per_page=5")
    except Exception:
        return None
    return data[0]["number"] if data else None


async def merge_pr(repo: str, number: int) -> bool:
    try:
        await _request("PUT", f"/repos/{repo}/pulls/{number}/merge", json={"merge_method": "squash"})
        return True
    except httpx.HTTPStatusError:
        return False


async def default_branch(repo: str) -> str:
    data = await _request("GET", f"/repos/{repo}")
    return data.get("default_branch", "main")


async def repo_exists(repo: str) -> bool:
    try:
        await _request("GET", f"/repos/{repo}")
        return True
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return False
        raise


async def list_user_repos(token: str) -> list[dict]:
    """Repos the given token can push to — for the project repo picker."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{API}/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        resp.raise_for_status()
        return [{"full_name": r["full_name"], "private": r["private"],
                 "updated_at": r.get("updated_at", "")}
                for r in resp.json() if r.get("permissions", {}).get("push")]


async def create_user_repo(token: str, name: str, private: bool = True) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{API}/user/repos",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json={"name": name, "private": private, "auto_init": True,
                  "description": "Created by devteam"})
        if resp.status_code >= 300:
            return False, f"{resp.status_code}: {resp.text[:200]}"
        return True, resp.json()["full_name"]


async def list_prs(repo: str) -> list[dict]:
    data = await _request("GET", f"/repos/{repo}/pulls?state=all&per_page=30")
    return [{"number": p["number"], "title": p["title"], "state": p["state"],
             "merged": bool(p.get("merged_at")), "url": p["html_url"]} for p in data]


async def list_branches(repo: str) -> list[str]:
    data = await _request("GET", f"/repos/{repo}/branches?per_page=50")
    return [b["name"] for b in data]




async def ensure_repo(repo: str) -> tuple[bool, str]:
    """Create the repo (private, initialized) if it doesn't exist.
    Returns (ok, note). `repo` is 'owner/name'; the token's user must be the owner
    (or the org must allow it). Personal repos are created for the token's own user."""
    if not enabled(repo):
        return False, "GitHub not configured"
    if await repo_exists(repo):
        return True, "repo already exists"
    owner, _, name = repo.partition("/")
    me = await _request("GET", "/user")
    body = {"name": name, "private": True, "auto_init": True,
            "description": "Created automatically by devteam"}
    try:
        if owner and owner.lower() != me.get("login", "").lower():
            # owner is an org the token can create in
            await _request("POST", f"/orgs/{owner}/repos", json=body)
        else:
            await _request("POST", "/user/repos", json=body)
        return True, f"created private repo {repo}"
    except httpx.HTTPStatusError as e:
        return False, f"could not create {repo}: {e.response.status_code} {e.response.text[:200]}"


async def list_tree(repo: str, ref: str = "") -> list[dict]:
    """Every file in the repo at `ref`, flat. The repo is the source of truth for
    what the team produced — a local checkout is a copy that can be stale."""
    if not ref:
        ref = await default_branch(repo)
    data = await _request("GET", f"/repos/{repo}/git/trees/{ref}?recursive=1")
    skip = (".git/", "node_modules/", "__pycache__/", ".venv/", "dist/", "build/")
    return [{"path": t["path"], "size": t.get("size", 0)}
            for t in data.get("tree", [])
            if t.get("type") == "blob" and not t["path"].startswith(skip)]


async def read_file(repo: str, path: str, ref: str = "") -> str:
    """One file's contents, decoded. Binary and oversized files are refused rather
    than returned as mojibake."""
    import base64
    q = f"?ref={ref}" if ref else ""
    data = await _request("GET", f"/repos/{repo}/contents/{path}{q}")
    if data.get("encoding") != "base64" or data.get("size", 0) > 400_000:
        raise ValueError("too large or not a text file")
    raw = base64.b64decode(data["content"])
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("not a text file")
