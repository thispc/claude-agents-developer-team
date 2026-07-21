"""Environments: build once, try it, promote the same artifact.

Production currently runs from a git checkout that `redeploy()` pulls into. Two
things follow from that, and both are bad:

- The tree is mutable, so the running app can be half-updated — the dashboard is
  read from disk on every request while the Python was loaded once at startup.
  That is the entire reason the "app is half-updated" banner had to exist.
- What you tested and what you shipped are *different builds*. Testing a branch
  and then pulling main means the artifact that went live was never run by anyone.

So: an environment is an **image**, not a directory.

    build(source)  ->  devteam:<ref>-<hash>        an immutable artifact
         │                    │
      preview               promote
         ▼                    ▼
    namespace devteam-<env>   namespace devteam
    own database, own host    the SAME image, not a rebuild

`promote` deliberately takes a tag that already exists rather than building from
main. The bytes that served your preview are the bytes that serve production —
there is no second build to drift, and no window where "tested" and "shipped"
mean different things.

Sources come from sandbox.snapshot, so a preview can be built from uncommitted
work: you do not have to merge something to find out whether it survives a real
deployment.
"""

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from . import config, sandbox, selfops

BUILD_DIR = sandbox.SANDBOX_DIR / "build"
STATE = sandbox.SANDBOX_DIR / "envs.json"
PROD_NAMESPACE = config._env("K8S_NAMESPACE", "devteam")
IMAGE_REPO = config._env("ENV_IMAGE_REPO", "devteam-conductor")
KIND_CLUSTER = config._env("KIND_CLUSTER", "devteam")


def _sh(*cmd: str, cwd: Path | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=timeout)


def _safe(name: str) -> str:
    """A DNS-1123 label: k8s namespaces and image tags are both fussy."""
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return (s or "env")[:40].strip("-")


def _state() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"images": [], "envs": {}}


def _save(st: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2))


def kubectl_ok() -> bool:
    return _sh("kubectl", "cluster-info", "--request-timeout=5s", timeout=20).returncode == 0


def docker_ok() -> bool:
    return _sh("docker", "version", "--format", "{{.Server.Version}}", timeout=20).returncode == 0


# --- build ---------------------------------------------------------------

def build(source: str, note: str = "") -> dict[str, Any]:
    """Snapshot `source` and build a conductor image from it.

    The tag carries the ref AND a content hash, so two builds of the same branch
    at different moments are different artifacts — which matters precisely because
    uncommitted work is allowed as a source and a branch name alone would lie.
    """
    if not docker_ok():
        return {"ok": False, "error": "Docker is not running, so nothing can be built"}
    tree = BUILD_DIR / "tree"
    kind, _, rest = source.partition(":")
    if kind == "live":
        src = selfops.LIVE_TREE
    elif kind == "workspace":
        if "/" in rest or ".." in rest:
            return {"ok": False, "error": f"refusing {rest!r}"}
        src = config.WORKSPACES_DIR / rest / "repo"
    elif kind == "ref":
        if not sandbox._safe_ref(rest):
            return {"ok": False, "error": f"refusing {rest!r} as a git ref"}
        src = selfops.LIVE_TREE      # snapshot then check out below
    else:
        return {"ok": False, "error": f"unknown source {source!r}"}

    ok, note_ = sandbox.snapshot(src, tree)
    if not ok:
        return {"ok": False, "error": note_}
    if kind == "ref":
        # snapshot dropped .git, so materialise the ref through a worktree instead
        wt = BUILD_DIR / "ref"
        _sh("git", "worktree", "prune", cwd=selfops.LIVE_TREE)
        _sh("git", "worktree", "remove", "--force", str(wt), cwd=selfops.LIVE_TREE)
        r = _sh("git", "worktree", "add", "--detach", str(wt), rest, cwd=selfops.LIVE_TREE)
        if r.returncode != 0:
            return {"ok": False, "error": f"could not check out {rest}: {r.stderr[-200:]}"}
        ok, note_ = sandbox.snapshot(wt, tree)
        _sh("git", "worktree", "remove", "--force", str(wt), cwd=selfops.LIVE_TREE)
        if not ok:
            return {"ok": False, "error": note_}

    digest = _tree_hash(tree)
    tag = f"{IMAGE_REPO}:{_safe(rest or kind)}-{digest[:10]}"
    dockerfile = selfops.LIVE_TREE / "deploy" / "Dockerfile.conductor"
    if not dockerfile.exists():
        return {"ok": False, "error": "deploy/Dockerfile.conductor is missing"}

    # Build for the architecture the CLUSTER runs, not the one this machine has.
    #
    # kind reuses the host's architecture, so local testing can never surface
    # this — but DOKS nodes are amd64 and a Mac builds arm64 by default, and that
    # image dies on the node with "exec format error", which says nothing about
    # architecture. When the target is a real cluster we cross-build with buildx
    # and push in the same step, because an amd64 image is not runnable here and
    # there is nothing to be gained by loading it locally first.
    if on_kind():
        r = _sh("docker", "build", "-f", str(dockerfile), "-t", tag, str(tree), timeout=1800)
    else:
        if not REGISTRY:
            return {"ok": False,
                    "error": "this cluster pulls from a registry, but DOCR_REGISTRY is "
                             "not set — there is nowhere to push the build"}
        r = _sh("docker", "buildx", "build", "--platform", config.DEPLOY_PLATFORM,
                "-f", str(dockerfile), "-t", registry_tag(tag), "--push",
                str(tree), timeout=3600)
    if r.returncode != 0:
        return {"ok": False, "error": "docker build failed",
                "log": (r.stdout + r.stderr)[-2500:]}

    st = _state()
    st["images"] = [i for i in st["images"] if i["tag"] != tag][:19]
    st["images"].insert(0, {"tag": tag, "source": source, "digest": digest,
                            "note": note, "built_at": time.time()})
    _save(st)
    return {"ok": True, "tag": tag, "digest": digest, "source": source}


def _tree_hash(tree: Path) -> str:
    """Content hash of what will actually be built.

    A branch name is not an identity when uncommitted work is a legal source —
    two builds of `live` an hour apart are different software and must not share
    a tag, or promoting "the one I tested" becomes a guess.
    """
    import hashlib
    h = hashlib.sha256()
    for p in sorted(tree.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(tree)
        if any(part in sandbox.SKIP for part in rel.parts):
            continue
        h.update(str(rel).encode())
        try:
            h.update(p.read_bytes())
        except Exception:
            pass
    return h.hexdigest()


# --- deploy --------------------------------------------------------------

REGISTRY = config._env("DOCR_REGISTRY")      # e.g. registry.digitalocean.com/my-reg


def on_kind() -> bool:
    """True when the cluster is the local kind one rather than a real provider."""
    ctx = _sh("kubectl", "config", "current-context", timeout=15).stdout.strip()
    return ctx.startswith("kind-")


def _publish(tag: str) -> tuple[bool, str]:
    """Get the image somewhere the cluster's nodes can pull it from.

    Two worlds, and confusing them is the classic way a rollout hangs on
    ImagePullBackOff with no useful message:
      - kind: nodes cannot see the host's Docker daemon, so the image is *loaded*
        into them. Nothing is pushed anywhere.
      - a real cluster (DOKS): nodes pull from a registry, so the image must be
        tagged into that registry and pushed.
    """
    if on_kind():
        r = _sh("kind", "load", "docker-image", tag, "--name", KIND_CLUSTER, timeout=900)
        return r.returncode == 0, (r.stderr or r.stdout)[-300:]
    if not REGISTRY:
        return False, ("this cluster pulls images from a registry, but DOCR_REGISTRY "
                       "is not set — nodes would have nowhere to fetch the build from")
    # Already pushed by build(), which cross-compiles straight to the registry —
    # an amd64 image cannot be held locally on an arm64 machine in any useful way.
    remote = registry_tag(tag)
    r = _sh("docker", "manifest", "inspect", remote, timeout=120)
    if r.returncode != 0:
        return False, (f"{remote} is not in the registry — build it for this cluster "
                       f"first (are you logged in to the registry?)")
    return True, f"in the registry as {remote}"


def ensure_pull_secret(namespace: str) -> tuple[bool, str]:
    """Copy the registry credential into a namespace so its nodes can pull.

    DOCR is private. Without this the pods sit in ImagePullBackOff, which surfaces
    as "image can't be found" and sends you hunting for a build problem that isn't
    there. The secret is copied from the default namespace rather than minted here,
    so the credential lives in exactly one place.
    """
    if not REGISTRY:
        return True, "no registry configured; nothing to copy"
    have = _sh("kubectl", "-n", namespace, "get", "secret", "docr-creds", timeout=30)
    if have.returncode == 0:
        return True, "already present"
    dump = _sh("kubectl", "-n", "default", "get", "secret", "docr-creds",
               "-o", "yaml", timeout=30)
    if dump.returncode != 0:
        return False, ("no 'docr-creds' secret in the default namespace to copy — "
                       "create it once with `kubectl create secret docker-registry`")
    body = re.sub(r"^\s*namespace:.*$", f"  namespace: {namespace}",
                  dump.stdout, count=1, flags=re.M)
    body = re.sub(r"^\s*(resourceVersion|uid|creationTimestamp):.*$", "",
                  body, flags=re.M)
    r = subprocess.run(["kubectl", "apply", "-f", "-"], input=body,
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stderr or r.stdout)[-200:]


def registry_tag(tag: str) -> str:
    """The name the CLUSTER will pull by. Local tags mean nothing to a DOKS node."""
    if not REGISTRY or tag.startswith(REGISTRY):
        return tag
    return f"{REGISTRY}/{tag}"


def cluster_tag(tag: str) -> str:
    return tag if on_kind() else registry_tag(tag)


def manifests(env: str, tag: str, namespace: str) -> str:
    """A whole environment: its own namespace, its own database, its own host.

    Its own database is the load-bearing part. A preview sharing production's
    volume would let a bad migration destroy real projects — and a migration that
    destroys data is exactly the failure a preview exists to catch.
    """
    host = f"{env}.{config.APPS_DOMAIN}" if config.APPS_DOMAIN else f"{env}.localhost"
    # A managed cluster bills for every LoadBalancer, and one per preview is how a
    # credit disappears into networking rather than compute. NodePort reaches the
    # same pod through the node's own IP for nothing; an ingress is worth it only
    # once there is a wildcard domain to hang the previews off.
    expose = "ClusterIP" if on_kind() else "NodePort"
    # DOCR is private, so the node needs credentials to pull. Without this the
    # rollout sits in ImagePullBackOff, which reports as "image not found" and
    # sends you looking for a build problem that isn't there.
    pull = ("      imagePullSecrets: [{name: docr-creds}]\n" if REGISTRY else "")
    ingress = f"""---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {{name: devteam-conductor, namespace: {namespace}}}
spec:
  ingressClassName: nginx
  rules:
    - host: {host}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: {{service: {{name: devteam-conductor, port: {{number: 80}}}}}}
""" if config.APPS_DOMAIN or on_kind() else ""
    return f"""apiVersion: v1
kind: Namespace
metadata: {{name: {namespace}, labels: {{devteam-env: "{env}"}}}}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {{name: devteam-data, namespace: {namespace}}}
spec:
  accessModes: [ReadWriteOnce]
  resources: {{requests: {{storage: 1Gi}}}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {{name: devteam-conductor, namespace: {namespace}, labels: {{devteam-env: "{env}"}}}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: devteam-conductor}}}}
  template:
    metadata: {{labels: {{app: devteam-conductor}}}}
    spec:
      containers:
        - name: conductor
          image: {tag}
          imagePullPolicy: IfNotPresent
          ports: [{{containerPort: 8000}}]
          env:
            - {{name: DB_PATH, value: /data/devteam.db}}
            - {{name: LAUNCHER, value: local}}
            - {{name: DEMO_MODE, value: "1"}}
            - {{name: DEMO_AUTH_MODE, value: subscription}}
            - {{name: ROOT_USERNAME, value: root}}
            - {{name: ROOT_PASSWORD, value: preview}}
            - {{name: DEVTEAM_ENV, value: "{env}"}}
          volumeMounts: [{{name: data, mountPath: /data}}]
          readinessProbe:
            httpGet: {{path: /api/health, port: 8000}}
            initialDelaySeconds: 3
            periodSeconds: 5
      volumes:
        - name: data
          persistentVolumeClaim: {{claimName: devteam-data}}
{pull}---
apiVersion: v1
kind: Service
metadata: {{name: devteam-conductor, namespace: {namespace}}}
spec:
  type: {expose}
  selector: {{app: devteam-conductor}}
  ports: [{{port: 80, targetPort: 8000}}]
{ingress}"""


def deploy_preview(tag: str, env: str) -> dict[str, Any]:
    """Roll `tag` out as its own environment, beside production and isolated from it."""
    if not kubectl_ok():
        return {"ok": False, "error": "no reachable Kubernetes cluster"}
    # Refuse before doing anything expensive OR destructive. The first version
    # compared the derived namespace against production, which can never match
    # (it is always prefixed) — so the guard was unreachable and a blank name
    # quietly deployed an environment called "env". Judge the caller's intent
    # instead: naming the production namespace means they meant production.
    raw = (env or "").strip()
    env = _safe(raw)
    if not raw or raw.lower() in (PROD_NAMESPACE.lower(), "production", "prod", "main"):
        return {"ok": False,
                "error": "name the environment something of its own — a preview may "
                         "not be called after production, and promote() is how "
                         "production changes"}
    namespace = f"{PROD_NAMESPACE}-{env}"

    published, note = _publish(tag)
    if not published:
        return {"ok": False, "error": f"could not make the image available to the "
                                      f"cluster: {note}"}
    body = manifests(env, cluster_tag(tag), namespace)
    # namespace must exist before the secret can be copied into it
    subprocess.run(["kubectl", "create", "namespace", namespace],
                   capture_output=True, text=True, timeout=60)
    sec_ok, sec_note = ensure_pull_secret(namespace)
    r = subprocess.run(["kubectl", "apply", "-f", "-"], input=body,
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-500:], "kind_loaded": loaded}

    w = _sh("kubectl", "-n", namespace, "rollout", "status",
            "deployment/devteam-conductor", "--timeout=180s", timeout=200)
    st = _state()
    st["envs"][env] = {"tag": tag, "namespace": namespace, "at": time.time(),
                       "host": f"{env}.{config.APPS_DOMAIN}" if config.APPS_DOMAIN
                       else f"{env}.localhost"}
    _save(st)
    return {"ok": w.returncode == 0, "env": env, "namespace": namespace, "tag": tag,
            "host": st["envs"][env]["host"], "published": note,
            "pull_secret": sec_note if not sec_ok else "ok",
            "error": "" if w.returncode == 0 else (w.stderr or w.stdout)[-400:]}


def promote(tag: str) -> dict[str, Any]:
    """Ship the exact image that was previewed. No rebuild, no pull, no drift.

    This is the whole point of the module: `redeploy()` pulls main into the live
    tree and restarts, so the thing that goes live was assembled at deploy time
    and nobody ever ran it. Here the artifact is already running somewhere and
    has been looked at — promotion just points production at it.
    """
    if not kubectl_ok():
        return {"ok": False, "error": "no reachable Kubernetes cluster"}
    known = {i["tag"] for i in _state()["images"]}
    if tag not in known:
        return {"ok": False,
                "error": "refusing to promote an image this platform did not build "
                         "and has no record of"}
    previous = _sh("kubectl", "-n", PROD_NAMESPACE, "get", "deployment",
                   "devteam-conductor", "-o",
                   "jsonpath={.spec.template.spec.containers[0].image}").stdout.strip()
    if not previous:
        return {"ok": False,
                "error": f"no devteam-conductor deployment in {PROD_NAMESPACE} to promote into"}
    # Ask what the container is actually called rather than assuming "conductor".
    # A production deployed from any other manifest would otherwise fail here with
    # kubectl's own words ('unable to find container named "conductor"'), which
    # reads like the image is wrong when the naming is.
    name = _sh("kubectl", "-n", PROD_NAMESPACE, "get", "deployment",
               "devteam-conductor", "-o",
               "jsonpath={.spec.template.spec.containers[0].name}").stdout.strip() or "conductor"
    published, note = _publish(tag)
    if not published:
        return {"ok": False, "error": f"the cluster could not be given the image: {note}"}
    r = _sh("kubectl", "-n", PROD_NAMESPACE, "set", "image",
            "deployment/devteam-conductor", f"{name}={cluster_tag(tag)}", timeout=120)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-400:]}
    w = _sh("kubectl", "-n", PROD_NAMESPACE, "rollout", "status",
            "deployment/devteam-conductor", "--timeout=180s", timeout=200)
    if w.returncode != 0:
        _sh("kubectl", "-n", PROD_NAMESPACE, "rollout", "undo",
            "deployment/devteam-conductor", timeout=120)
        return {"ok": False, "rolled_back": True, "from": previous,
                "error": (w.stderr or w.stdout)[-400:]}
    st = _state()
    st["production"] = {"tag": tag, "previous": previous, "at": time.time()}
    _save(st)
    return {"ok": True, "tag": tag, "previous": previous}


def rollback() -> dict[str, Any]:
    """k8s keeps the previous ReplicaSet, so this is one command and no guesswork."""
    if not kubectl_ok():
        return {"ok": False, "error": "no reachable Kubernetes cluster"}
    r = _sh("kubectl", "-n", PROD_NAMESPACE, "rollout", "undo",
            "deployment/devteam-conductor", timeout=120)
    return {"ok": r.returncode == 0, "detail": (r.stdout or r.stderr)[-300:]}


def destroy(env: str) -> dict[str, Any]:
    raw = (env or "").strip()
    if not raw or raw.lower() in (PROD_NAMESPACE.lower(), "production", "prod", "main"):
        return {"ok": False, "error": "that is production"}
    env = _safe(raw)
    namespace = f"{PROD_NAMESPACE}-{env}"
    r = _sh("kubectl", "delete", "namespace", namespace, "--wait=false", timeout=120)
    st = _state()
    st["envs"].pop(env, None)
    _save(st)
    return {"ok": r.returncode == 0, "detail": (r.stdout or r.stderr)[-200:]}


def overview() -> dict[str, Any]:
    """Every environment and every artifact, so promotion is a choice not a guess."""
    st = _state()
    live = []
    if kubectl_ok():
        r = _sh("kubectl", "get", "ns", "-l", "devteam-env", "-o",
                "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}")
        live = [n for n in r.stdout.splitlines() if n.strip()]
    return {"docker": docker_ok(), "kubernetes": kubectl_ok(),
            "kind": on_kind() if kubectl_ok() else False, "registry": REGISTRY,
            "production": st.get("production"), "images": st["images"][:10],
            "envs": st["envs"], "live_namespaces": live,
            "prod_namespace": PROD_NAMESPACE}
