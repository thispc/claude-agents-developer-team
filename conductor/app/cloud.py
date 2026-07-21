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
