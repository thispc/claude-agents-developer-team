"""How this instance ships itself: environments, images, staging, the sandbox,
and the redeploy/rollback pair.

All of it root-gated, because every endpoint here changes what code this
server runs. The one exception to the cookie gate is /internal/self-verify,
which carries its own token: it is the image testing itself over plain HTTP,
the replacement for a cross-namespace Kubernetes exec whose broken websocket
path hid four different failures behind one message.
"""

import asyncio
import hmac

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel

from .. import bus, cloud, config, db, envs, sandbox, selfops
from .base import _root, router


class EnvBuild(BaseModel):
    source: str
    note: str = ""


class EnvDeploy(BaseModel):
    tag: str
    env: str


class EnvTag(BaseModel):
    tag: str


@router.get("/api/self/envs")
def envs_overview(request: Request) -> dict:
    _root(request)
    return envs.overview()


@router.post("/api/self/envs/build")
def envs_build(body: EnvBuild, request: Request) -> dict:
    _root(request)
    r = envs.build(body.source.strip(), body.note.strip())
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "build failed"))
    return r


@router.post("/api/self/envs/deploy")
def envs_deploy(body: EnvDeploy, request: Request) -> dict:
    _root(request)
    r = envs.deploy_preview(body.tag.strip(), body.env.strip())
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "deploy failed"))
    return r


@router.post("/api/self/envs/promote")
def envs_promote(body: EnvTag, request: Request) -> dict:
    """Point production at an image that has already been previewed."""
    _root(request)
    r = envs.promote(body.tag.strip())
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "promotion failed"))
    bus.emit(0, None, "system", "promoted", {"tag": r["tag"], "from": r.get("previous")})
    return r


@router.get("/api/self/healing")
def self_healing(request: Request) -> dict:
    """What the platform has fixed, tried and failed to fix, on its own.

    Exists so "you were not asked about it" does not mean "you cannot find out
    about it" — the whole bargain of routine autonomy is that it stays visible.
    """
    u = _root(request)
    pid = selfops.ensure_project(u["id"])
    kinds = {"self_healed": "healed", "canary_failed": "rejected a bad build",
             "auto_update": "took a new version", "self_update": "took a new version",
             "notified": "raised an issue", "digest_filed": "filed a sprint digest",
             "rolled_back": "rolled back"}
    items = []
    for e in reversed(db.list_events(pid, limit=300) + db.list_events(0, limit=200)):
        if e["kind"] not in kinds:
            continue
        try:
            payload = db.json.loads(e["payload"])
        except Exception:
            payload = {"note": str(e["payload"])[:200]}
        items.append({"kind": e["kind"], "what": kinds[e["kind"]],
                      "at": e["ts"], "detail": payload})
        if len(items) >= 40:
            break
    return {"items": items, "project_id": pid}


@router.get("/api/self/instance")
def self_instance(request: Request) -> dict:
    """What this instance is and whether it can ship a new version of itself."""
    _root(request)
    return cloud.describe()


@router.get("/api/self/images")
async def self_images(request: Request) -> dict:
    """What CI has published, and whether a newer one is waiting."""
    _root(request)
    imgs = await cloud.available_images()
    return {"images": imgs, "candidate": await cloud.newer_than_running(),
            "auto_update": cloud.AUTO_UPDATE, "busy": cloud.busy()}


class SelfImage(BaseModel):
    image: str
    force: bool = False


@router.post("/api/self/update")
def self_update(body: SelfImage, request: Request) -> dict:
    """Point our own Deployment at a new image. This pod is then replaced."""
    _root(request)
    r = cloud.self_update(body.image.strip(), body.force)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "self-update failed"))
    bus.emit(0, None, "system", "self_update", {"from": r.get("from"), "to": r["to"]})
    return r


class StagingReq(BaseModel):
    image: str


@router.post("/api/self/staging")
def staging_deploy(body: StagingReq, request: Request) -> dict:
    """Run an image in staging: real credentials, its own identity."""
    _root(request)
    r = cloud.staging_deploy(body.image.strip())
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "staging deploy failed"))
    bus.emit(0, None, "system", "staging_deployed", {"image": r["image"]})
    return r


@router.post("/internal/self-verify")
def self_verify(x_worker_token: str | None = Header(None)) -> dict:
    """Run my own test suite and report the exit code.

    Production used to reach into staging with a Kubernetes exec to do this. That
    needed cross-namespace RBAC, and the Python client's websocket path is broken
    against urllib3 2.x in a way that swallows the real error — the client's own
    exception handler crashes decoding a None body, so every failure arrived as
    "'NoneType' object has no attribute 'decode'" whatever had actually gone
    wrong. Four separate causes hid behind that one message.

    An instance running its own suite over plain HTTP has none of those problems,
    and is a truer test anyway: it is the image testing itself, in its own
    environment, exactly as it would run.
    """
    # Its own token, not the worker one — see config.VERIFY_TOKEN.
    if not x_worker_token or not hmac.compare_digest(x_worker_token, config.VERIFY_TOKEN):
        raise HTTPException(401, "bad verify token")
    import subprocess
    try:
        r = subprocess.run(
            ["python", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
             "-m", "not live and not hostonly"],
            cwd="/app", capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "output": "",
                "error": "the suite did not finish within 30 minutes"}
    out = (r.stdout or "") + (r.stderr or "")
    return {"ok": r.returncode == 0, "exit_code": r.returncode,
            "image": cloud.current_image(), "output": out[-2000:]}


@router.post("/api/self/staging/verify")
def staging_verify(body: StagingReq, request: Request) -> dict:
    """Run the test suite inside staging — the check a boot canary cannot make."""
    _root(request)
    r = cloud.staging_verify(body.image.strip())
    bus.emit(0, None, "system",
             "staging_passed" if r.get("ok") else "staging_failed",
             {"image": body.image, "output": (r.get("output") or "")[:600]})
    return r


@router.post("/api/self/staging/promote-check")
def staging_promote_check(body: StagingReq, request: Request) -> dict:
    """Deploy an image to staging and run its own tests against it, in one step.

    One call because the two halves are only meaningful together: a deployment
    nobody tested proves nothing, and a test run against a different build proves
    less than nothing.
    """
    _root(request)
    dep = cloud.staging_deploy(body.image.strip())
    if not dep.get("ok"):
        raise HTTPException(400, f"staging would not come up: {dep.get('error')}")
    ver = cloud.staging_verify(body.image.strip())
    bus.emit(0, None, "system",
             "staging_passed" if ver.get("ok") else "staging_failed",
             {"image": body.image, "output": (ver.get("output") or "")[:600]})
    return {"deployed": dep, "verified": ver,
            "may_promote": bool(ver.get("ok")),
            "note": ("production can now adopt this image" if ver.get("ok")
                     else "production will refuse this image while the gate is on")}


@router.delete("/api/self/staging")
def staging_teardown(request: Request) -> dict:
    _root(request)
    return cloud.staging_teardown()


@router.post("/api/self/update/rollback")
def self_update_rollback(request: Request) -> dict:
    _root(request)
    r = cloud.rollback()
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "rollback failed"))
    return r


@router.post("/api/self/envs/rollback")
def envs_rollback(request: Request) -> dict:
    """k8s keeps the previous ReplicaSet, so this needs no artifact and no guesswork."""
    _root(request)
    r = envs.rollback()
    if not r.get("ok"):
        raise HTTPException(400, r.get("detail") or r.get("error", "rollback failed"))
    bus.emit(0, None, "system", "rolled_back", {"detail": r.get("detail", "")})
    return r


@router.delete("/api/self/envs/{env}")
def envs_destroy(env: str, request: Request) -> dict:
    _root(request)
    return envs.destroy(env)


class SandboxReq(BaseModel):
    ref: str          # a source id from sandbox.sources(): live | workspace:… | ref:…


@router.get("/api/self/sandbox")
def sandbox_status(request: Request) -> dict:
    _root(request)
    return {**sandbox.status(), "sources": sandbox.sources()}


@router.post("/api/self/sandbox")
def sandbox_start(body: SandboxReq, request: Request) -> dict:
    """Boot the candidate build beside the live one so it can be clicked through.

    A diff says the code is plausible; only running it says the app still works.
    """
    _root(request)
    res = sandbox.start(body.ref.strip())
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "the sandbox would not start"))
    bus.emit(0, None, "system", "sandbox_started",
             {"ref": res["ref"], "commit": res["commit"], "port": res["port"]})
    return res


@router.delete("/api/self/sandbox")
def sandbox_stop(request: Request) -> dict:
    _root(request)
    return sandbox.stop()


@router.post("/api/self/redeploy")
async def self_redeploy(request: Request, force: bool = False) -> dict:
    _root(request)
    res = selfops.redeploy(force=force)
    if res.get("ok"):
        bus.emit(0, None, "system", "self_redeploy",
                 {"from": res["from"]["commit"], "to": res["to"]["commit"]})
        # Give the response time to reach the browser before the process is replaced.
        asyncio.get_event_loop().call_later(1.5, selfops.restart_process)
    return res


@router.post("/api/self/rollback")
async def self_rollback(request: Request) -> dict:
    _root(request)
    res = selfops.rollback()
    if res.get("ok"):
        asyncio.get_event_loop().call_later(1.5, selfops.restart_process)
    return res
