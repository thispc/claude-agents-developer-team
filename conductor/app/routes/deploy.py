"""Running a project's real app: deploy, roll back, and per-branch previews.

One module because these are the same power at different scopes — the deployed
default branch, one branch beside it, and the cluster-level undo. The module
shares a name with app.deploy deliberately: this is that engine's HTTP surface
and nothing else.
"""

from fastapi import HTTPException, Request

from .. import config, deploy
from .base import owned_project, router


@router.get("/api/projects/{project_id}/deploy")
def deploy_status(project_id: int, request: Request) -> dict:
    owned_project(project_id, request)
    return deploy.status(project_id)


@router.post("/api/projects/{project_id}/deploy")
async def deploy_app(project_id: int, request: Request, mode: str = "",
                     workspace: str = "") -> dict:
    """Build and run the project's real app — backend included.

    `workspace` runs an agent's own checkout rather than the merged default
    branch, so a change can be exercised before it is committed.
    """
    owned_project(project_id, request)
    mode = mode or ("k8s" if config.LAUNCHER == "k8s" else "local")
    if mode == "k8s":
        return await deploy.deploy_k8s(project_id)
    return await deploy.deploy_local(project_id, workspace.strip())


@router.post("/api/projects/{project_id}/deploy/rollback")
async def rollback_app(project_id: int, request: Request) -> dict:
    """Put back the last version of this project's app that came up healthy."""
    owned_project(project_id, request)
    r = await deploy.rollback(project_id)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "rollback failed"))
    return r


@router.get("/api/projects/{project_id}/deploy/history")
def deploy_history(project_id: int, request: Request) -> dict:
    """Every deploy attempt of this app, healthy or not — a record of only the
    successes cannot tell you three attempts from one branch died the same way."""
    owned_project(project_id, request)
    return {"history": deploy.history(project_id)}


@router.delete("/api/projects/{project_id}/deploy")
def undeploy_app(project_id: int, request: Request) -> dict:
    owned_project(project_id, request)
    return {"status": deploy.stop(project_id)}


# --- per-branch previews of a user's app ---
#
# The platform could always preview its own candidate builds; a user could only
# deploy their app and roll it back. These are the sideways move: a branch running
# on its own name and its own host, beside the deployed app rather than over it,
# so "does this work?" is answerable while the change is still reviewable.

@router.get("/api/projects/{project_id}/previews")
def list_previews(project_id: int, request: Request) -> dict:
    owned_project(project_id, request)
    return {"previews": deploy.previews(project_id)}


@router.post("/api/projects/{project_id}/previews")
async def start_preview(project_id: int, request: Request, branch: str,
                        mode: str = "") -> dict:
    """Run one branch of this project's app, without touching what is deployed."""
    owned_project(project_id, request)
    if not deploy.safe_branch(branch):
        raise HTTPException(400, f"{branch!r} is not a branch name this can check out")
    mode = mode or ("k8s" if config.LAUNCHER == "k8s" else "local")
    r = (await deploy.deploy_k8s(project_id, branch) if mode == "k8s"
         else await deploy.deploy_local(project_id, "", branch))
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "preview failed"))
    return r


@router.delete("/api/projects/{project_id}/previews")
def stop_preview(project_id: int, request: Request, branch: str) -> dict:
    """Tear one preview down. Without a branch this would stop the deployed app,
    which is a different button and should stay one."""
    owned_project(project_id, request)
    if not deploy.safe_branch(branch):
        raise HTTPException(400, f"{branch!r} is not a branch name")
    return {"status": deploy.stop(project_id, branch)}


@router.post("/api/projects/{project_id}/deploy/rollback/cluster")
async def rollback_app_on_cluster(project_id: int, request: Request,
                                  branch: str = "") -> dict:
    """Undo the last rollout on the cluster, without rebuilding anything.

    Distinct from the local rollback above, which redeploys a remembered source.
    Rebuilding to go backwards would produce a NEW image from the same source
    rather than the one that was known to work.
    """
    owned_project(project_id, request)
    r = await deploy.rollback_k8s(project_id, branch)
    if not r.get("ok"):
        raise HTTPException(400, r.get("detail") or r.get("error", "rollback failed"))
    return r
