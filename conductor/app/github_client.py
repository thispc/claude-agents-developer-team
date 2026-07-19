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
    return bool(config.GITHUB_TOKEN and repo)


async def _request(method: str, path: str, **kwargs) -> dict:
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


async def merge_pr(repo: str, number: int) -> bool:
    try:
        await _request("PUT", f"/repos/{repo}/pulls/{number}/merge", json={"merge_method": "squash"})
        return True
    except httpx.HTTPStatusError:
        return False


async def default_branch(repo: str) -> str:
    data = await _request("GET", f"/repos/{repo}")
    return data.get("default_branch", "main")
