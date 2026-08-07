"""Signing in, per-user settings, and the user's own GitHub repos.

Everything a person does before they have a project: get a session cookie,
store their own credentials (every user brings their own AI keys — nobody runs
on the operator's subscription), prove those credentials work, and pick or
create the repo their team will push to.
"""

from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from .. import auth, config, credcheck, github_client, providers
from .base import current_user, router


class Login(BaseModel):
    username: str
    password: str


class Settings(BaseModel):
    github_token: str | None = None
    anthropic_api_key: str | None = None
    claude_oauth_token: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None


class NewRepo(BaseModel):
    name: str
    private: bool = True


@router.post("/api/login")
def login(body: Login, response: Response) -> dict:
    wait = auth.locked_out(body.username)
    if wait:
        # Say it plainly. A locked account that reports "wrong password" sends the
        # owner hunting for a typo while an attacker learns nothing either way.
        raise HTTPException(429, f"too many failed attempts — try again in {wait}s")
    u = auth.verify(body.username, body.password)
    if not u:
        raise HTTPException(401, "wrong username or password")
    token = auth.start_session(u["id"])
    response.set_cookie("devteam_session", token, httponly=True, samesite="lax", max_age=30 * 86400)
    return {"username": u["username"], "is_root": bool(u["is_root"])}


@router.post("/api/signup")
def signup(body: Login, response: Response) -> dict:
    """Anyone can create an account, but they bring their own AI credentials —
    a new user never runs on the operator's subscription."""
    name = body.username.strip().lower()
    if len(name) < 3 or len(body.password) < 6:
        raise HTTPException(400, "username needs 3+ chars, password 6+")
    if auth.get_user_by_name(name):
        raise HTTPException(400, "that username is taken")
    uid = auth.create_user(name, body.password)
    token = auth.start_session(uid)
    response.set_cookie("devteam_session", token, httponly=True, samesite="lax", max_age=30 * 86400)
    return {"username": name, "is_root": False, "needs_credentials": True}


@router.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
    auth.end_session(request.cookies.get("devteam_session"))
    response.delete_cookie("devteam_session")
    return {"ok": True}


@router.get("/api/me")
def me(request: Request) -> dict:
    u = auth.user_for_token(request.cookies.get("devteam_session"))
    if not u:
        return {"signed_in": False}
    s = auth.get_settings(u)
    return {"signed_in": True, "username": u["username"], "is_root": bool(u["is_root"]),
            "has_ai_credentials": auth.has_own_ai_credentials(u),
            "may_self_repair": config.may_self_repair(u["username"], bool(u["is_root"])),
            "canvas_v2": config.CANVAS_V2,
            "module_graph": config.MODULE_GRAPH,
            "settings": auth.redacted(s)}


@router.post("/api/settings")
def save_settings(body: Settings, request: Request) -> dict:
    u = current_user(request)
    auth.save_settings(u["id"], body.model_dump(exclude_none=True))
    return auth.redacted(auth.get_settings(auth.get_user(u["id"])))


class VerifyCred(BaseModel):
    kind: str
    value: str = ""      # blank = check the one already stored


@router.post("/api/settings/verify")
async def verify_credential(body: VerifyCred, request: Request) -> dict:
    """Prove a credential works before the user finds out by losing a project.

    Checks the value being typed if there is one, otherwise the stored value —
    so 'Check' is useful both while entering a key and long afterwards.

    A custom endpoint verifies through the same button and returns the same
    shape, because it fails the same way: an endpoint that silently does not
    answer is indistinguishable from a key that was never valid. Its `value` is
    the endpoint id, since what is being proved is a URL, a key and a model list
    together rather than any one string.
    """
    u = current_user(request)
    if body.kind == credcheck.ENDPOINT_KIND:
        ep = providers.endpoint(providers.CUSTOM_PREFIX + body.value.strip(),
                                auth.get_settings(u))
        if not ep:
            raise HTTPException(404, "no such endpoint")
        return await credcheck.check_endpoint(ep)
    if body.kind not in credcheck.KINDS:
        raise HTTPException(400, f"cannot verify {body.kind!r}")
    value = body.value.strip() or auth.get_settings(u).get(body.kind, "")
    return await credcheck.check(body.kind, value)


class CustomEndpoint(BaseModel):
    id: str = ""                    # blank = derived from the label
    label: str = ""
    base_url: str
    # None means "leave the stored key alone". The browser is never sent a key, so
    # a user editing the model list would otherwise submit an empty field and
    # un-authenticate a working server; "" still clears it, deliberately.
    api_key: str | None = None
    key_header: str = "Authorization"
    models: list[str] = []


@router.get("/api/settings/endpoints")
def list_endpoints(request: Request) -> dict:
    """The user's own inference endpoints, keys withheld."""
    u = current_user(request)
    return {"endpoints": auth.redacted(auth.get_settings(u))["custom_endpoints"]}


@router.post("/api/settings/endpoints")
def save_endpoint(body: CustomEndpoint, request: Request) -> dict:
    """Add or replace one OpenAI-compatible endpoint.

    Saving does not check it. Verification is a separate call because it costs a
    real request against the user's server, and a Settings dialog that reaches out
    on every keystroke is one people learn to avoid.
    """
    u = current_user(request)
    try:
        auth.save_endpoint(u, body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"endpoints": auth.redacted(
        auth.get_settings(auth.get_user(u["id"])))["custom_endpoints"]}


@router.delete("/api/settings/endpoints/{endpoint_id}")
def delete_endpoint(endpoint_id: str, request: Request) -> dict:
    u = current_user(request)
    if not auth.delete_endpoint(u, endpoint_id):
        raise HTTPException(404, "no such endpoint")
    return {"endpoints": auth.redacted(
        auth.get_settings(auth.get_user(u["id"])))["custom_endpoints"]}


@router.get("/api/github/repos")
async def list_my_repos(request: Request) -> dict:
    """The signed-in user's repos, for the project picker."""
    u = current_user(request)
    token = auth.get_settings(u).get("github_token", "")
    if not token:
        raise HTTPException(400, "no GitHub token set — add one in Settings")
    return {"repos": await github_client.list_user_repos(token)}


@router.post("/api/github/repos")
async def create_my_repo(body: NewRepo, request: Request) -> dict:
    u = current_user(request)
    token = auth.get_settings(u).get("github_token", "")
    if not token:
        raise HTTPException(400, "no GitHub token set — add one in Settings")
    ok, result = await github_client.create_user_repo(token, body.name, body.private)
    if not ok:
        raise HTTPException(400, result)
    return {"repo": result}
