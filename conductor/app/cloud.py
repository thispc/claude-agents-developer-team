"""Self-repair when the platform lives in a container, not a git checkout.

`selfops.redeploy()` was written for a laptop: `git pull` into the tree the process
runs from, then restart. In a cluster none of that exists — the image has no
`.git`, no docker, no kubectl, and the filesystem is thrown away on restart. So
the last step of self-repair, the one that actually ships the fix, has no way to
happen.

In a cluster the equivalent is smaller and safer:

    a merged PR  ->  CI builds an image      (outside; see .github/workflows)
                 ->  the conductor patches its own Deployment's image
                 ->  Kubernetes replaces the pod, which kills this process

Nothing is pulled, nothing is built here, and the running code is never mutated
in place — the old pod dies and a new one starts from an image that already
existed. That is also why it is safer than the laptop version: a rollout that
fails to become ready is undone by Kubernetes, and the previous ReplicaSet is
still there to roll back to.

The conductor talks to the API with its ServiceAccount token, which is mounted
into every pod. No kubectl binary is needed.
"""

import os
from pathlib import Path
from typing import Any

from . import config, db

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
DEPLOYMENT = config._env("SELF_DEPLOYMENT", "devteam-conductor")
# Baked in by the build so a container can answer "what am I running?" without a
# git checkout. Blank on a laptop, where selfops reads git directly.
BUILD_COMMIT = config._env("DEVTEAM_COMMIT")
BUILD_TAG = config._env("DEVTEAM_IMAGE")


def in_cluster() -> bool:
    """True when running as a pod, which is what makes the git path impossible."""
    return SA_DIR.joinpath("token").exists()


def namespace() -> str:
    try:
        return SA_DIR.joinpath("namespace").read_text().strip() or "devteam"
    except Exception:
        return config._env("K8S_NAMESPACE", "devteam")


def _api():
    from kubernetes import client, config as kconfig
    kconfig.load_incluster_config()
    return client.AppsV1Api()


def current_image() -> str:
    """The image this Deployment is set to run — the honest answer to 'what version
    am I?', because the env var could be stale after someone patched us."""
    if not in_cluster():
        return ""
    try:
        d = _api().read_namespaced_deployment(DEPLOYMENT, namespace())
        return d.spec.template.spec.containers[0].image
    except Exception:
        return ""


def busy() -> list[str]:
    """Work that a restart would destroy.

    Patching the Deployment kills this pod, and every worker is a child process of
    it — so an agent 80 turns into a task loses all of it, and the run is spent.
    The platform knows what is in flight; it should not need a human to remember.
    """
    out = []
    for p in db.list_projects():
        for t in db.list_tasks(p["id"]):
            if t["status"] in ("running", "queued"):
                out.append(f"#{t['seq']} {t['role']} on '{p['name']}'")
    return out


def can_self_update() -> dict[str, Any]:
    """Whether shipping a new image to ourselves would work, and why not if not."""
    reasons = []
    if not in_cluster():
        reasons.append("not running in Kubernetes — use the local redeploy instead")
    elif not current_image():
        reasons.append(
            f"cannot read the '{DEPLOYMENT}' Deployment. The pod's ServiceAccount "
            f"probably lacks get/patch on deployments, or is not attached at all "
            f"(serviceAccountName in the manifest)")
    live = busy()
    return {"ok": not reasons and not live, "reasons": reasons, "busy": live,
            "image": current_image(), "namespace": namespace() if in_cluster() else ""}


CANARY = f"{DEPLOYMENT}-canary"
CANARY_TIMEOUT = int(config._env("CANARY_TIMEOUT", "120"))


def canary(image: str) -> dict[str, Any]:
    """Prove an image can actually boot before making it the platform.

    Kubernetes already protects against the worst case — a pod that never becomes
    ready leaves the old one serving — but only *after* the rollout has begun, and
    only for failures the readiness probe catches. An image that starts, passes a
    health check and is subtly broken still replaces a working platform.

    So the new image gets run first, beside the live one, as its own short-lived
    Deployment with a scratch database and DEMO_MODE on. It is asked the same
    question the readiness probe asks, and torn down either way. Nothing about the
    running platform is touched: the canary has its own name, writes its database
    to its own container filesystem (thrown away with the pod), and has no Service,
    so nothing can route to it.
    """
    if not in_cluster():
        return {"ok": False, "error": "not in a cluster"}
    from kubernetes import client
    api, core = _api(), client.CoreV1Api()
    ns = namespace()
    body = client.V1Deployment(
        metadata=client.V1ObjectMeta(name=CANARY, namespace=ns,
                                     labels={"app": CANARY, "devteam-canary": "true"}),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"app": CANARY}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"app": CANARY}),
                spec=client.V1PodSpec(
                    service_account_name=DEPLOYMENT,
                    image_pull_secrets=[client.V1LocalObjectReference(name="docr-creds")],
                    restart_policy="Always",
                    containers=[client.V1Container(
                        name="conductor", image=image,
                        # A scratch database: a migration that destroys data does it
                        # here, which is the entire reason for running this at all.
                        env=[client.V1EnvVar(name="DB_PATH", value="/tmp/canary.db"),
                             client.V1EnvVar(name="DEMO_MODE", value="1"),
                             client.V1EnvVar(name="ROOT_USERNAME", value="root"),
                             client.V1EnvVar(name="ROOT_PASSWORD", value="canary")],
                        readiness_probe=client.V1Probe(
                            http_get=client.V1HTTPGetAction(path="/api/health", port=8000),
                            initial_delay_seconds=3, period_seconds=3),
                    )]))))
    _delete_canary()
    try:
        api.create_namespaced_deployment(ns, body)
    except Exception as e:
        return {"ok": False, "error": f"could not start the canary: {e}"}

    import time as _t
    deadline = _t.time() + CANARY_TIMEOUT
    last = ""
    while _t.time() < deadline:
        _t.sleep(3)
        try:
            d = api.read_namespaced_deployment(CANARY, ns)
            if (d.status.ready_replicas or 0) >= 1:
                _delete_canary()
                return {"ok": True, "note": "the new image booted and answered "
                                            "/api/health on its own database"}
            pods = core.list_namespaced_pod(ns, label_selector=f"app={CANARY}")
            for p in pods.items:
                for cs in (p.status.container_statuses or []):
                    w = cs.state.waiting
                    if w and w.reason:
                        last = f"{w.reason}: {(w.message or '')[:160]}"
                        # These never resolve by waiting; fail now rather than at
                        # the timeout, so the reason is the real one.
                        if w.reason in ("ImagePullBackOff", "ErrImagePull",
                                        "CrashLoopBackOff", "CreateContainerError"):
                            _delete_canary()
                            return {"ok": False, "error": last}
        except Exception as e:
            last = str(e)[:160]
    _delete_canary()
    return {"ok": False, "error": f"the new image did not become ready within "
                                  f"{CANARY_TIMEOUT}s. {last}"}


def _delete_canary() -> None:
    try:
        from kubernetes import client
        _api().delete_namespaced_deployment(
            CANARY, namespace(),
            body=client.V1DeleteOptions(propagation_policy="Foreground"))
    except Exception:
        pass          # already gone, which is the state we wanted anyway


def self_update(image: str, force: bool = False) -> dict[str, Any]:
    """Point our own Deployment at `image`. Kubernetes then replaces this pod.

    Note what does NOT happen: no health check afterwards, because the process
    doing the patching is the process being replaced. Kubernetes owns that part —
    a pod that never becomes ready leaves the old ReplicaSet in place, and
    `rollout undo` returns to it. Verifying our own success is not something we
    can be trusted with once we are dead.
    """
    check = can_self_update()
    if not check["ok"] and not force:
        if check["busy"]:
            return {"ok": False, "error": "agents are working right now; restarting "
                                          "would throw that away",
                    "busy": check["busy"]}
        return {"ok": False, "error": "; ".join(check["reasons"])}
    if not image or ":" not in image:
        return {"ok": False, "error": f"{image!r} is not an image reference"}
    previous = current_image()
    if image == previous:
        return {"ok": False, "error": "already running that image"}
    # Prove it boots before it becomes the platform. Skipped only with force,
    # which is for getting OUT of a bad state — where the canary would be a second
    # thing to go wrong while the platform is already broken.
    if not force:
        c = canary(image)
        if not c.get("ok"):
            from . import bus
            bus.emit(0, None, "system", "canary_failed",
                     {"image": image, "why": c.get("error", "")})
            return {"ok": False, "error": f"the new image did not pass a trial run, "
                                          f"so it was not adopted: {c.get('error')}",
                    "canary": c}
    try:
        _api().patch_namespaced_deployment(
            DEPLOYMENT, namespace(),
            {"spec": {"template": {"spec": {"containers": [
                {"name": "conductor", "image": image}]}}}})
    except Exception as e:
        return {"ok": False, "error": f"could not patch the Deployment: {e}"}
    return {"ok": True, "from": previous, "to": image,
            "note": "Kubernetes is replacing this pod. This process is about to end; "
                    "if the new image cannot start, the old pod stays up and "
                    "`kubectl rollout undo` returns to it."}


def rollback() -> dict[str, Any]:
    """Return to the previous image without needing to know what it was.

    Reads it off the ReplicaSet history rather than trusting anything we stored:
    our own record of 'previous' does not survive the restart that a bad rollout
    causes, which is exactly when it would be needed.
    """
    if not in_cluster():
        return {"ok": False, "error": "not running in Kubernetes"}
    try:
        from kubernetes import client
        rs = client.AppsV1Api().list_namespaced_replica_set(
            namespace(), label_selector=f"app={DEPLOYMENT}")
        old = sorted((r for r in rs.items
                      if (r.spec.replicas or 0) == 0 and r.spec.template.spec.containers),
                     key=lambda r: r.metadata.creation_timestamp, reverse=True)
        if not old:
            return {"ok": False, "error": "no previous version recorded to roll back to"}
        return self_update(old[0].spec.template.spec.containers[0].image, force=True)
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- noticing that a newer version exists ---------------------------------
#
# Without this the loop is autonomous but not unattended: CI publishes an image
# and the platform sits there until a human pastes the tag. Polling the registry
# closes that, and the registry is the right thing to watch rather than GitHub —
# an image that exists is a thing you can actually run, whereas a merged commit
# might still be building or might have failed to build at all.

REGISTRY = config._env("DOCR_REGISTRY")
AUTO_UPDATE = config._env("AUTO_UPDATE") == "1"
CHECK_SECONDS = int(config._env("UPDATE_CHECK_SECONDS", "300"))


def _registry_name() -> str:
    # registry.digitalocean.com/devteam-pulkit -> devteam-pulkit
    return REGISTRY.rstrip("/").split("/")[-1] if REGISTRY else ""


async def available_images(limit: int = 10) -> list[dict[str, Any]]:
    """Tags in the registry, newest first. Empty when we cannot look, never raises."""
    import httpx
    # A read-only registry token, NOT the account token. The account token can
    # create and destroy clusters; a pod that only needs to list image tags has no
    # business holding it. DOCR issues scoped, expiring credentials for exactly
    # this, so least privilege costs nothing here.
    token = config._env("DOCR_READ_TOKEN") or config._env("DIGITALOCEAN_API_TOKEN")
    reg = _registry_name()
    if not token or not reg:
        return []
    url = (f"https://api.digitalocean.com/v2/registry/{reg}"
           f"/repositories/devteam-conductor/tags?per_page=50")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            tags = r.json().get("tags", [])
    except Exception:
        return []
    tags.sort(key=lambda t: t.get("updated_at", ""), reverse=True)
    running = current_image()
    return [{"tag": f"{REGISTRY}/devteam-conductor:{t['tag']}",
             "short": t["tag"],
             "updated_at": t.get("updated_at", ""),
             "running": f"{REGISTRY}/devteam-conductor:{t['tag']}" == running}
            for t in tags[:limit]]


async def newer_than_running() -> dict[str, Any] | None:
    """The most recent image that is not the one we are running.

    Deliberately "most recent", not "any newer" — a rollback publishes nothing, so
    the newest tag is the intended head. It is only a candidate; taking it is a
    separate decision, because taking it destroys whatever the agents are doing.
    """
    imgs = await available_images()
    if not imgs or imgs[0].get("running"):
        return None
    return imgs[0]


async def check_and_maybe_update() -> dict[str, Any]:
    """The unattended path: adopt a newer image when it is safe to.

    Safe means idle. A restart kills every worker — each is a child process of
    this one — so an agent eighty turns into a task would lose all of it and the
    run would be spent for nothing. Waiting costs a few minutes; not waiting costs
    the work.
    """
    if not in_cluster():
        return {"checked": False, "reason": "not in a cluster"}
    cand = await newer_than_running()
    if not cand:
        return {"checked": True, "update": None}
    live = busy()
    if live:
        return {"checked": True, "update": cand, "deferred": True, "busy": live}
    if not AUTO_UPDATE:
        return {"checked": True, "update": cand, "deferred": True,
                "reason": "AUTO_UPDATE is off; adopt it from the Improve page"}
    from . import bus
    bus.emit(0, None, "system", "auto_update",
             {"to": cand["short"], "note": "a newer image was published and nothing "
                                           "was running, so it was adopted"})
    return {"checked": True, "update": cand, "applied": self_update(cand["tag"])}


async def watch() -> None:
    """Poll the registry forever. Started at boot; harmless outside a cluster."""
    import asyncio
    if not in_cluster() or not REGISTRY:
        return
    while True:
        await asyncio.sleep(CHECK_SECONDS)
        try:
            await check_and_maybe_update()
        except Exception:
            pass          # a registry blip must never take the conductor down


def describe() -> dict[str, Any]:
    """What this instance is, for the UI — works on a laptop and in a pod."""
    return {
        "in_cluster": in_cluster(),
        "namespace": namespace() if in_cluster() else "",
        "image": current_image(),
        "build_commit": BUILD_COMMIT,
        "build_tag": BUILD_TAG,
        "can_self_update": can_self_update(),
    }
