"""The mechanics of shipping an image, shared by the platform and by users' apps.

There were two of these. `envs.py` grew content-hash tags, an image identity that
survives uncommitted work, and rollback from the ReplicaSet history Kubernetes
already keeps — because the platform is the one thing whose breakage nobody else
can fix. `deploy.py` shipped users' apps with none of it: build, apply, hope. The
asymmetry was never decided, it is just where the work happened to be done, and
the consequence is that a user's app could be deployed and rolled back but never
*checked* before it took over from a version that worked.

What is genuinely shared turns out to be small, and all of it is here:

    tree_hash / content_tag   an image is identified by its CONTENT
    build_image               ...and a failed build yields NO tag at all
    canary                    ...which is run beside the live one before it wins
    promote                   ...then pointed at a NAMED deployment
    rollback                  ...or undone from the history k8s already keeps

What is deliberately *not* shared is the shape of what gets deployed. A preview
of the platform carries its own database and demo credentials; a user's app
carries a port, an ingress host and a teardown label. Those manifests stay with
their owners — merging them would produce one function with a flag for every
difference, which is how a shared module becomes worse than two separate ones.
Only the verbs live here.

Everything drives `kubectl`, because both callers run on a host that has one.
`cloud.py` does a similar job through the Kubernetes API instead, and that is not
duplication to be tidied away: it patches the Deployment it is itself running in,
from inside a pod that has no kubectl and never will.
"""

import hashlib
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from . import config, shell

BUILD_TIMEOUT = int(config._env("IMAGE_BUILD_TIMEOUT", "3600"))
CANARY_TIMEOUT = int(config._env("ROLLOUT_CANARY_TIMEOUT", "150"))
# Waiting reasons that never resolve by waiting. Failing on them immediately
# means the error the caller reads is the real one rather than "timed out".
TERMINAL = ("ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff",
            "CreateContainerError", "CreateContainerConfigError", "InvalidImageName")


def sh(*cmd: str, cwd: Path | None = None, stdin: str | None = None,
       timeout: int = 900) -> subprocess.CompletedProcess:
    """shell.sh with a higher ceiling: image builds and kubectl waits are
    legitimately slow. The error contract — a missing binary is a failed run,
    never an exception — was learned here and now lives in shell.sh."""
    return shell.sh(*cmd, cwd=cwd, stdin=stdin, timeout=timeout)


def safe_label(name: str) -> str:
    """A DNS-1123 label: k8s object names and image tags are both fussy."""
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return (s or "env")[:40].strip("-")


def tree_hash(tree: Path, skip: set[str] | None = None) -> str:
    """Content hash of what will actually be built.

    A branch name is not an identity when uncommitted work is a legal source —
    two builds of the same branch an hour apart are different software and must
    not share a tag, or "promote the one I tested" becomes a guess.
    """
    if skip is None:
        from . import sandbox
        skip = sandbox.SKIP
    h = hashlib.sha256()
    for p in sorted(tree.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(tree)
        if any(part in skip for part in rel.parts):
            continue
        h.update(str(rel).encode())
        try:
            h.update(p.read_bytes())
        except Exception:
            pass
    return h.hexdigest()


def content_tag(repo: str, ref: str, digest: str) -> str:
    """`repo:<ref>-<hash>` — readable enough to recognise, unique enough to trust.

    The ref alone would collide across rebuilds; the hash alone would be
    unreadable in a rollout log at the moment you most need to read it.
    """
    return f"{repo}:{safe_label(ref)}-{digest[:10]}"


def build_image(dockerfile: str, context: Path, tag: str, *, platform: str = "",
                push: bool = False, timeout: int = BUILD_TIMEOUT) -> dict[str, Any]:
    """Build `tag`, and on failure return NO tag at all.

    That last clause is most of the reason this is one function. An earlier
    version read the tag back out of docker's own output with `awk '{print $3}'`;
    on a failed build the column it landed on held the word "docker", production
    was set to an image literally called `docker`, and the cluster sat in
    ImagePullBackOff saying nothing about a build. Here the tag is known before
    the build starts and is only ever returned when the build succeeded — there is
    nothing to parse and so nothing to misparse.

    `platform` cross-builds. A remote cluster runs whatever its nodes are, not
    what this host is: DOKS nodes are amd64, a Mac builds arm64 by default, and
    that image dies on the node with "exec format error" — a message that says
    nothing about architecture. `push` sends it straight to the registry from
    buildx, because a foreign-architecture image is not runnable here and loading
    it locally first buys nothing.
    """
    if platform:
        cmd = ["docker", "buildx", "build", "--platform", platform,
               "-f", dockerfile, "-t", tag,
               "--push" if push else "--load", str(context)]
    else:
        cmd = ["docker", "build", "-f", dockerfile, "-t", tag, str(context)]
    r = sh(*cmd, cwd=context, timeout=timeout)
    if r.returncode != 0:
        blob = (r.stdout or "") + (r.stderr or "")
        # Name the common cause rather than making the caller read a build log.
        # Registry credentials are short-lived by design, so "unauthorized" here
        # means an expired login far more often than it means anything else.
        why = ("the registry login has expired — run `doctl registry login`, or "
               "fetch fresh docker credentials for the registry"
               if "unauthorized" in blob.lower() or "authentication required" in blob.lower()
               else "docker build failed")
        return {"ok": False, "error": why, "log": blob[-2500:], "command": " ".join(cmd[:3])}
    return {"ok": True, "tag": tag}


def deployment_name(name: str, default: str = "") -> str:
    """The Deployment to act on, refusing anything kubectl would read as something else.

    Arguments are passed as argv so there is no shell to inject into, but a name
    beginning with '-' is still parsed by kubectl as an option, and a name with a
    '/' in it changes which RESOURCE TYPE is addressed. Both turn "act on the
    wrong deployment" into "act on something else entirely".
    """
    n = (name or default).strip()
    return n if n and "/" not in n and not n.startswith("-") else ""


# --- proving an image works before it is allowed to matter -----------------

def _canary_manifest(namespace: str, name: str, image: str, port: int, health: str,
                     pull_secret: str, env: dict[str, str]) -> str:
    """A Deployment and nothing else. No Service is the point: nothing can route
    to a candidate, so a canary that comes up half-working still serves no one."""
    envs = "\n".join(f'            - {{name: {k}, value: "{v}"}}' for k, v in env.items())
    pull = f"      imagePullSecrets: [{{name: {pull_secret}}}]\n" if pull_secret else ""
    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {namespace}
  labels: {{app: {name}, devteam/canary: "true"}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: {name}}}}}
  template:
    metadata: {{labels: {{app: {name}, devteam/canary: "true"}}}}
    spec:
{pull}      containers:
        - name: canary
          image: {image}
          imagePullPolicy: IfNotPresent
          ports: [{{containerPort: {port}}}]
          env:
{envs}
          readinessProbe:
            httpGet: {{path: {health}, port: {port}}}
            initialDelaySeconds: 3
            periodSeconds: 3
          resources:
            requests: {{cpu: 50m, memory: 96Mi}}
            limits: {{cpu: 500m, memory: 512Mi}}
"""


def canary(namespace: str, name: str, image: str, *, port: int = 8000,
           health: str = "/", pull_secret: str = "", env: dict[str, str] | None = None,
           timeout: int = CANARY_TIMEOUT, poll: int = 3) -> dict[str, Any]:
    """Run `image` beside the live one and ask it the same question, then delete it.

    Kubernetes already protects the worst case — a pod that never becomes ready
    leaves the old one serving — but only *after* the rollout has begun, and only
    for what the readiness probe catches. Doing it first, under a different name
    with no Service, costs one pod's lifetime and means a broken image never
    reaches the object that is actually serving traffic.

    The probe here is deliberately the same one the real Deployment uses. A canary
    that is easier to satisfy than the rollout it gates proves nothing: the image
    would pass here and then hang the rollout for the same reason.
    """
    env = dict(env or {})
    body = _canary_manifest(namespace, name, image, port, health, pull_secret, env)
    delete(namespace, name)          # a leftover from a crashed run would be read as this one
    r = sh("kubectl", "apply", "-f", "-", stdin=body, timeout=120)
    if r.returncode != 0:
        return {"ok": False, "error": f"could not start the canary: "
                                      f"{(r.stderr or r.stdout)[-300:]}"}
    deadline = time.time() + timeout
    last = ""
    try:
        while time.time() < deadline:
            time.sleep(poll)
            ready = sh("kubectl", "-n", namespace, "get", "deployment", name, "-o",
                       "jsonpath={.status.readyReplicas}", timeout=30).stdout.strip()
            if ready and ready not in ("0", ""):
                return {"ok": True, "image": image,
                        "note": "the image started and answered its own readiness probe "
                                "before anything was pointed at it"}
            w = sh("kubectl", "-n", namespace, "get", "pods", "-l", f"app={name}", "-o",
                   "jsonpath={range .items[*].status.containerStatuses[*]}"
                   "{.state.waiting.reason}|{.state.waiting.message}{'\\n'}{end}",
                   timeout=30).stdout
            for line in w.splitlines():
                reason, _, message = line.partition("|")
                if not reason.strip():
                    continue
                last = f"{reason}: {message[:160]}"
                if reason.strip() in TERMINAL:
                    return {"ok": False, "error": last, "image": image}
        return {"ok": False, "image": image,
                "error": f"the image did not become ready within {timeout}s. {last}"}
    finally:
        delete(namespace, name)


def delete(namespace: str, name: str) -> None:
    sh("kubectl", "-n", namespace, "delete", "deployment", name,
       "--ignore-not-found", "--wait=false", timeout=60)


# --- promotion and its undo ------------------------------------------------

def running_image(namespace: str, deployment: str) -> tuple[str, str]:
    """(image, container name) of a live Deployment, or ("", "") if it is not there.

    The container name is asked for rather than assumed. A production deployed
    from any other manifest would otherwise fail with kubectl's own words —
    'unable to find container named "conductor"' — which reads as if the image
    were wrong when it is the naming that is.
    """
    image = sh("kubectl", "-n", namespace, "get", "deployment", deployment, "-o",
               "jsonpath={.spec.template.spec.containers[0].image}").stdout.strip()
    if not image:
        return "", ""
    name = sh("kubectl", "-n", namespace, "get", "deployment", deployment, "-o",
              "jsonpath={.spec.template.spec.containers[0].name}").stdout.strip()
    return image, name


def promote(namespace: str, deployment: str, image: str, *, container: str = "",
            previous: str = "", timeout: int = 180) -> dict[str, Any]:
    """Point a NAMED Deployment at `image`, and undo it if it does not come up.

    Named, never selected. `kubectl set image deployment/<name>` exits 1 for a
    Deployment that is not there (checked against the live cluster: 'Error from
    server (NotFound)') and for a container name it cannot find — but it exits 0
    and prints NOTHING when the target is expressed as `--all` or as a label
    selector that matches nothing. A promotion that no-ops is worse than one that
    errors: it reports the new image as live while the old one keeps serving.

    This also never applies a manifest. Applying one that pins an image is how
    production gets silently rolled BACK to whatever the file happens to say.
    """
    if not container or not previous:
        found_image, found_container = running_image(namespace, deployment)
        previous = previous or found_image
        container = container or found_container
    if not previous:
        return {"ok": False, "deployment": deployment,
                "error": f"no '{deployment}' deployment in {namespace} to promote into"}
    r = sh("kubectl", "-n", namespace, "set", "image", f"deployment/{deployment}",
           f"{container or 'app'}={image}", timeout=120)
    if r.returncode != 0:
        return {"ok": False, "deployment": deployment, "error": (r.stderr or r.stdout)[-400:]}
    w = sh("kubectl", "-n", namespace, "rollout", "status", f"deployment/{deployment}",
           f"--timeout={timeout}s", timeout=timeout + 20)
    if w.returncode != 0:
        sh("kubectl", "-n", namespace, "rollout", "undo", f"deployment/{deployment}",
           timeout=120)
        return {"ok": False, "rolled_back": True, "from": previous,
                "deployment": deployment, "error": (w.stderr or w.stdout)[-400:]}
    return {"ok": True, "image": image, "previous": previous, "deployment": deployment}


def rollback(namespace: str, deployment: str) -> dict[str, Any]:
    """Kubernetes still holds the previous ReplicaSet, so this needs no bookkeeping.

    Reading the target off the cluster rather than off anything we stored is the
    point: our own record of "previous" does not survive the restart a bad rollout
    causes, which is exactly when it would be needed.
    """
    r = sh("kubectl", "-n", namespace, "rollout", "undo", f"deployment/{deployment}",
           timeout=120)
    return {"ok": r.returncode == 0, "deployment": deployment,
            "detail": (r.stdout or r.stderr)[-300:]}
