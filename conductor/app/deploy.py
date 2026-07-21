"""Run a project's app for real — backend and all.

The static preview only serves files, so an app with a server (`/api/...`) 404s
the moment you use it. This runs the actual application:

- **locally**: build, then run it as a subprocess on its own port
- **in production**: build an image and roll out a Deployment + Service on k8s

Two decisions worth knowing:

**Every app gets its own origin (port or Service), never a path under ours.**
A deployed app calls `/api/weather` absolutely. Proxied under `/app/11/`, that
request would land on the control plane instead — broken, and an app written by
an agent would be able to reach our API with the operator's cookies. A separate
port has no such ambiguity.

**Locally, the child gets a scrubbed environment.** Agent-written code runs with
the operator's user account, so it is started with a minimal env: no
ANTHROPIC_API_KEY, no GITHUB_TOKEN, no WORKER_TOKEN. It cannot read the
credentials of the platform that built it.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from . import bus, config, db

DEPLOY_DIR = Path(config._env("DEPLOY_DIR", str(config.ROOT / "deployments")))
PORT_RANGE_START = int(config._env("DEPLOY_PORT_START", "8600"))
BUILD_TIMEOUT = int(config._env("DEPLOY_BUILD_TIMEOUT", "600"))
BOOT_TIMEOUT = int(config._env("DEPLOY_BOOT_TIMEOUT", "90"))

# project_id -> {"proc": Popen, "port": int, "started": float, "spec": dict}
RUNNING: dict[int, dict[str, Any]] = {}


# --------------------------------------------------------------------------
# Detection: how does this repo actually run?
# --------------------------------------------------------------------------

def detect(root: Path) -> dict[str, Any]:
    """Work out how to build and start whatever the team built.

    Returns a runspec; kind == 'static' means there is no server to run and the
    existing static preview is the right tool.
    """
    if (root / "Dockerfile").exists():
        return {"kind": "docker", "why": "a Dockerfile is present",
                "build": None, "run": None, "health": "/"}

    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
        except Exception:
            data = {}
        scripts = data.get("scripts") or {}
        # `npm ci` fails outright when the lockfile is out of sync with package.json,
        # which agent-written repos often are — fall back rather than dead-end.
        install = ("npm ci --omit=dev || npm install --omit=dev"
                   if (root / "package-lock.json").exists() else "npm install --omit=dev")
        if "start" in scripts:
            return {"kind": "node", "why": "package.json defines a start script",
                    "build": install,
                    "run": "npm start", "health": "/"}
        for entry in ("server.js", "index.js", "app.js", "main.js", "src/server.js"):
            if (root / entry).exists():
                return {"kind": "node", "why": f"{entry} looks like the server entrypoint",
                        "build": install, "run": f"node {entry}", "health": "/"}
        if "build" in scripts:
            return {"kind": "node-static",
                    "why": "package.json builds a static bundle but defines no server",
                    "build": f"{install} && npm run build", "run": None, "health": "/"}

    reqs = root / "requirements.txt"
    if reqs.exists() or (root / "pyproject.toml").exists():
        install = "pip install -r requirements.txt" if reqs.exists() else "pip install -e ."
        text = reqs.read_text().lower() if reqs.exists() else ""
        for entry in ("main.py", "app.py", "server.py", "api.py", "run.py"):
            if not (root / entry).exists():
                continue
            mod = entry[:-3]
            if "fastapi" in text or "uvicorn" in text:
                return {"kind": "python", "why": f"FastAPI/uvicorn app in {entry}",
                        "build": install,
                        "run": f"python -m uvicorn {mod}:app --host 0.0.0.0 --port $PORT",
                        "health": "/"}
            return {"kind": "python", "why": f"{entry} looks like the entrypoint",
                    "build": install, "run": f"python {entry}", "health": "/"}

    if (root / "index.html").exists() or (root / "public" / "index.html").exists():
        return {"kind": "static", "why": "static files only — no server to run",
                "build": None, "run": None, "health": "/"}

    return {"kind": "unknown", "why": "could not work out how to start this project",
            "build": None, "run": None, "health": "/"}


def _free_port() -> int:
    for p in range(PORT_RANGE_START, PORT_RANGE_START + 200):
        if p in {r["port"] for r in RUNNING.values()}:
            continue
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("no free port available for a deployment")


def _child_env(port: int) -> dict[str, str]:
    """A deliberately minimal environment for agent-written code.

    Inheriting os.environ would hand the app the operator's Anthropic key, GitHub
    token and worker token. It gets what a web app legitimately needs and nothing else.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "PORT": str(port),
        "NODE_ENV": "production",
        "PYTHONUNBUFFERED": "1",
        "HOST": "0.0.0.0",
    }


def pid_file(project_id: int) -> Path:
    return DEPLOY_DIR / str(project_id) / "running.json"


def _adopt(project_id: int) -> dict[str, Any] | None:
    """Re-attach to an app still running after the conductor restarted.

    Without this, a restart loses the handle and the app keeps holding its port
    forever with no way to stop it from the UI.
    """
    f = pid_file(project_id)
    if not f.exists():
        return None
    try:
        rec = json.loads(f.read_text())
        os.kill(rec["pid"], 0)              # raises if the process is gone
    except Exception:
        f.unlink(missing_ok=True)
        return None
    import httpx
    try:                                     # confirm it's still serving, not a recycled pid
        httpx.get(f"http://127.0.0.1:{rec['port']}/", timeout=2)
    except Exception:
        f.unlink(missing_ok=True)
        return None
    entry = {"proc": _Adopted(rec["pid"]), "port": rec["port"],
             "started": rec.get("started", time.time()), "spec": rec.get("spec", {})}
    RUNNING[project_id] = entry
    return entry


class _Adopted:
    """Stand-in for a Popen we no longer own, after a conductor restart."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None

    def poll(self):
        try:
            os.kill(self.pid, 0)
            return None
        except Exception:
            return -1

    def wait(self, timeout=None):
        return -1

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except Exception:
            pass


def workdir(project_id: int) -> Path:
    return DEPLOY_DIR / str(project_id) / "repo"


def log_path(project_id: int) -> Path:
    return DEPLOY_DIR / str(project_id) / "deploy.log"


def _log(project_id: int, line: str) -> None:
    p = log_path(project_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")


async def sync_from_workspace(project_id: int, workspace: str) -> tuple[bool, str]:
    """Take the code from an agent's own checkout, uncommitted work included.

    Deploying only from `main` means a change cannot be tried until it has been
    committed, pushed, reviewed and merged — so the first time anyone runs it is
    after it has already shipped. A QA role needs the opposite loop: run what the
    agent has right now, find the problem, send it back within the same sprint.
    """
    from . import sandbox
    if "/" in workspace or ".." in workspace:
        return False, f"refusing {workspace!r} as a workspace name"
    src = config.WORKSPACES_DIR / workspace / "repo"
    ok, note = sandbox.snapshot(src, workdir(project_id))
    if not ok:
        return False, note
    dirty = sandbox._dirty(src)
    return True, (f"copied {workspace} as it is on disk"
                  + (f" ({dirty} uncommitted change(s))" if dirty else ""))


async def sync_code(project_id: int) -> tuple[bool, str]:
    """Fresh checkout of the default branch — deployments always use latest main."""
    from . import github_client

    project = db.get_project(project_id)
    if not project or not project["repo"]:
        return False, "no repo attached to this project"
    if not github_client.enabled(project["repo"]):
        return False, "GitHub is not configured, so the code can't be fetched"
    dest = workdir(project_id)
    url = github_client.clone_url(project["repo"], config.GITHUB_TOKEN)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    r = subprocess.run(["git", "clone", "--depth", "1", url, str(dest)],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return False, f"clone failed: {r.stderr[-300:]}"
    return True, "checked out the latest main"


def _wait_healthy(port: int, proc: subprocess.Popen, path: str) -> tuple[bool, str]:
    """Poll until the app answers, or it dies, or we run out of patience."""
    import httpx

    deadline = time.time() + BOOT_TIMEOUT
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            return False, f"the app exited immediately with code {proc.returncode}"
        try:
            r = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=3)
            # Any HTTP answer means the server is listening; 404 on / is fine.
            return True, f"responding on port {port} (HTTP {r.status_code})"
        except Exception as e:
            last = str(e)
        time.sleep(1)
    return False, f"no response on port {port} within {BOOT_TIMEOUT}s ({last[:120]})"


def stop(project_id: int) -> str:
    # Tear down a k8s deployment first (label was set at apply time). Harmless when
    # there is nothing there; essential so cloud resources don't leak.
    if shutil.which("kubectl"):
        subprocess.run(["kubectl", "delete", "deployment,service,ingress",
                        "-n", config.K8S_NAMESPACE,
                        "-l", f"devteam/project={project_id}", "--ignore-not-found"],
                       capture_output=True, text=True)
    r = RUNNING.pop(project_id, None) or (_adopt(project_id) and RUNNING.pop(project_id, None))
    pid_file(project_id).unlink(missing_ok=True)
    if not r:
        return "stopped"
    proc = r["proc"]
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    _log(project_id, "stopped by the operator")
    return "stopped"


def _history_file(project_id: int) -> Path:
    return workdir(project_id).parent / f"deploy-history-{project_id}.json"


def _remember(project_id: int, entry: dict) -> None:
    """What was deployed, and from where. Without this a broken deploy has nothing
    to go back TO — the platform learned this for itself and the project side
    never got it."""
    try:
        f = _history_file(project_id)
        hist = json.loads(f.read_text()) if f.exists() else []
        hist.insert(0, entry)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(hist[:10], indent=2))
    except Exception:
        pass


def history(project_id: int) -> list[dict]:
    try:
        return json.loads(_history_file(project_id).read_text())
    except Exception:
        return []


async def rollback(project_id: int) -> dict[str, Any]:
    """Redeploy the last source that actually came up healthy.

    A project deploy could previously only go forwards: if a deploy broke the app
    the only move was to fix it and deploy again, with the app down in between.
    """
    prev = [h for h in history(project_id) if h.get("ok")]
    if len(prev) < 2:
        return {"ok": False, "error": "no earlier healthy deploy to return to"}
    target = prev[1]
    res = await deploy_local(project_id, target.get("workspace", ""))
    res["rolled_back_to"] = target
    return res


async def deploy_local(project_id: int, workspace: str = "") -> dict[str, Any]:
    """Build and run the app as a subprocess on its own port.

    `workspace` runs an agent's checkout instead of the merged default branch, so
    work can be exercised before it is committed.
    """
    stop(project_id)
    log_path(project_id).parent.mkdir(parents=True, exist_ok=True)
    log_path(project_id).write_text("")

    ok, note = (await sync_from_workspace(project_id, workspace) if workspace
                else await sync_code(project_id))
    _source = workspace or "default branch"
    _log(project_id, note)
    if not ok:
        return {"ok": False, "error": note}

    root = workdir(project_id)
    spec = detect(root)
    _log(project_id, f"detected: {spec['kind']} — {spec['why']}")
    if spec["kind"] == "static":
        return {"ok": False, "spec": spec,
                "error": "This project is static — there is no server to run. "
                         "Use the static preview instead."}
    if spec["kind"] == "unknown":
        return {"ok": False, "spec": spec,
                "error": "Couldn't work out how to start this project: no Dockerfile, "
                         "no package.json start script, no Python entrypoint."}
    if spec["kind"] == "node-static":
        return {"ok": False, "spec": spec,
                "error": "This project builds a static bundle and has no server. "
                         "Use the static preview instead."}

    port = _free_port()

    if spec["kind"] == "docker":
        if not shutil.which("docker"):
            return {"ok": False, "spec": spec,
                    "error": "This project has a Dockerfile but docker isn't installed here."}
        tag = f"devteam-app-{project_id}:latest"
        _log(project_id, f"docker build -t {tag}")
        b = subprocess.run(["docker", "build", "-t", tag, "."], cwd=root,
                           capture_output=True, text=True, timeout=BUILD_TIMEOUT)
        _log(project_id, (b.stdout + b.stderr)[-4000:])
        if b.returncode != 0:
            return {"ok": False, "spec": spec,
                    "error": f"docker build failed: {b.stderr[-300:]}"}
        cmd = ["docker", "run", "--rm", "-p", f"{port}:{port}",
               "-e", f"PORT={port}", "--name", f"devteam-app-{project_id}", tag]
    else:
        if spec["build"]:
            _log(project_id, f"build: {spec['build']}")
            b = subprocess.run(spec["build"], cwd=root, shell=True, capture_output=True,
                               text=True, timeout=BUILD_TIMEOUT, env=_child_env(port))
            _log(project_id, (b.stdout + b.stderr)[-4000:])
            if b.returncode != 0:
                return {"ok": False, "spec": spec,
                        "error": f"build failed: {(b.stderr or b.stdout)[-300:]}"}
        cmd = ["/bin/sh", "-c", spec["run"].replace("$PORT", str(port))]

    _log(project_id, f"starting on port {port}: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    logf = log_path(project_id).open("a")
    proc = subprocess.Popen(cmd, cwd=root, stdout=logf, stderr=subprocess.STDOUT,
                            env=_child_env(port), start_new_session=True)
    healthy, note = _wait_healthy(port, proc, spec.get("health", "/"))
    if not healthy:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        tail = log_path(project_id).read_text()[-1200:]
        _log(project_id, f"FAILED: {note}")
        # Record the failure too. A history of only successes cannot tell you that
        # the last three attempts from this branch all died the same way.
        _remember(project_id, {"ok": False, "workspace": workspace,
                               "source": _source, "at": time.time(),
                               "error": note[:200]})
        return {"ok": False, "spec": spec, "error": note, "log": tail,
                "can_roll_back": len([h for h in history(project_id) if h.get("ok")]) >= 1}

    RUNNING[project_id] = {"proc": proc, "port": port, "started": time.time(), "spec": spec}
    pid_file(project_id).write_text(json.dumps(
        {"pid": proc.pid, "port": port, "started": time.time(), "spec": spec}))
    _log(project_id, f"live: {note}")
    url = f"http://localhost:{port}"
    bus.emit(project_id, None, "system", "app_deployed",
             {"mode": "local", "url": url, "kind": spec["kind"]})
    _remember(project_id, {"ok": True, "workspace": workspace, "source": _source,
                           "at": time.time(), "url": url})
    return {"ok": True, "url": url, "port": port, "spec": spec, "mode": "local",
            "source": _source,
            "can_roll_back": len([h for h in history(project_id) if h.get("ok")]) >= 2}


# --------------------------------------------------------------------------
# Production: a real Deployment + Service on the cluster
# --------------------------------------------------------------------------

def _kubectl(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], input=stdin, capture_output=True,
                          text=True, timeout=300)


def ingress_available() -> bool:
    """Is there a shared ingress controller we can hang apps off?"""
    r = _kubectl("get", "ingressclass", "-o", "jsonpath={.items[*].metadata.name}")
    return bool(r.stdout.strip())


def app_host(project_id: int) -> str:
    domain = config.APPS_DOMAIN
    return f"app-{project_id}.{domain}" if domain else ""


def manifests(project_id: int, image: str, port: int = 8080,
              use_ingress: bool = True) -> str:
    """Deployment + Service, and an Ingress when a shared controller exists.

    A DigitalOcean regional HTTP load balancer costs $12/month *per load balancer*.
    Giving every deployed app `type: LoadBalancer` therefore bills per app — ten
    apps is $120/month of pure plumbing. One ingress controller fronts every app
    behind a single load balancer, routed by hostname, for $12 total.
    """
    name = f"devteam-app-{project_id}"
    host = app_host(project_id)
    svc_type = "ClusterIP" if (use_ingress and host) else "LoadBalancer"
    doc = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {config.K8S_NAMESPACE}
  labels: {{app: {name}, devteam/project: "{project_id}"}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: {name}}}}}
  template:
    metadata:
      labels: {{app: {name}}}
    spec:
      containers:
      - name: app
        image: {image}
        imagePullPolicy: IfNotPresent
        ports: [{{containerPort: {port}}}]
        env:
        - {{name: PORT, value: "{port}"}}
        readinessProbe:
          httpGet: {{path: /, port: {port}}}
          initialDelaySeconds: 3
          periodSeconds: 5
          failureThreshold: 12
        resources:
          requests: {{cpu: 50m, memory: 96Mi}}
          limits: {{cpu: 500m, memory: 512Mi}}
---
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {config.K8S_NAMESPACE}
  labels: {{app: {name}, devteam/project: "{project_id}"}}
spec:
  type: {svc_type}
  selector: {{app: {name}}}
  ports: [{{port: 80, targetPort: {port}}}]
"""
    if svc_type == "ClusterIP":
        doc += f"""---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {name}
  namespace: {config.K8S_NAMESPACE}
  labels: {{app: {name}, devteam/project: "{project_id}"}}
spec:
  ingressClassName: nginx
  rules:
  - host: {host}
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: {name}
            port: {{number: 80}}
"""
    return doc


async def deploy_k8s(project_id: int) -> dict[str, Any]:
    """Build an image and roll the app out on the cluster."""
    if not shutil.which("kubectl"):
        return {"ok": False, "error": "kubectl isn't available on the conductor host"}
    ok, note = await sync_code(project_id)
    _log(project_id, note)
    if not ok:
        return {"ok": False, "error": note}

    root = workdir(project_id)
    spec = detect(root)
    _log(project_id, f"detected: {spec['kind']} — {spec['why']}")
    if spec["kind"] in ("static", "node-static", "unknown"):
        return {"ok": False, "spec": spec,
                "error": f"nothing to deploy: {spec['why']}"}

    # A cluster can only run an image it can pull. A managed cluster (DOKS) needs a
    # registry it has credentials for; a local kind cluster can be handed the image
    # directly, which is what makes this testable without paying for a registry.
    registry = config._env("DEPLOY_REGISTRY", "")
    ctx = _kubectl("config", "current-context").stdout.strip()
    local_cluster = ctx.startswith("kind-")
    if not registry and not local_cluster:
        return {"ok": False, "spec": spec,
                "error": "set DEPLOY_REGISTRY (e.g. registry.digitalocean.com/yourreg) "
                         "so the built image can be pushed where the cluster can pull it"}
    tag = str(int(time.time()))
    image = f"{registry}/devteam-app-{project_id}:{tag}" if registry \
        else f"devteam-app-{project_id}:{tag}"

    dockerfile = "Dockerfile"
    if spec["kind"] != "docker":
        # Written under our own name so it never shadows the repo's own file and
        # never makes a later detect() believe the project ships a Dockerfile.
        dockerfile = "Dockerfile.devteam"
        (root / dockerfile).write_text(_generated_dockerfile(spec))
        _log(project_id, "no Dockerfile in the repo — generated one from the detected runtime")

    build = ["docker", "build", "-f", dockerfile, "-t", image, "."]
    if registry and config.DEPLOY_PLATFORM:
        # A remote cluster runs whatever the cloud's nodes are, not what this host
        # is. Building natively on an ARM Mac produces an image that crash-loops
        # with "exec format error" on amd64 nodes.
        build = ["docker", "buildx", "build", "--platform", config.DEPLOY_PLATFORM,
                 "-f", dockerfile, "-t", image, "--load", "."]
        _log(project_id, f"cross-building for {config.DEPLOY_PLATFORM} "
                         f"(this host is {os.uname().machine})")
    steps = [build]
    if registry:
        steps.append(["docker", "push", image])
    else:
        steps.append(["kind", "load", "docker-image", image,
                      "--name", ctx.removeprefix("kind-")])
    for cmd in steps:
        _log(project_id, " ".join(cmd))
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=BUILD_TIMEOUT)
        _log(project_id, (r.stdout + r.stderr)[-3000:])
        if r.returncode != 0:
            return {"ok": False, "spec": spec,
                    "error": f"{' '.join(cmd[:2])} failed: {(r.stderr or r.stdout)[-300:]}"}

    use_ingress = bool(config.APPS_DOMAIN) and ingress_available()
    if not use_ingress:
        _log(project_id, "no APPS_DOMAIN/ingress controller — falling back to a "
                         "per-app LoadBalancer (billed separately by the cloud)")
    r = _kubectl("apply", "-f", "-",
                 stdin=manifests(project_id, image, use_ingress=use_ingress))
    _log(project_id, r.stdout + r.stderr)
    if r.returncode != 0:
        return {"ok": False, "spec": spec, "error": f"kubectl apply failed: {r.stderr[-300:]}"}

    name = f"devteam-app-{project_id}"
    _kubectl("rollout", "status", f"deployment/{name}", "-n", config.K8S_NAMESPACE,
             "--timeout=180s")
    if use_ingress:
        url = f"http://{app_host(project_id)}"
        ip = ""
    else:
        ip = _kubectl("get", "svc", name, "-n", config.K8S_NAMESPACE, "-o",
                      "jsonpath={.status.loadBalancer.ingress[0].ip}").stdout.strip()
        url = f"http://{ip}" if ip else ""
    bus.emit(project_id, None, "system", "app_deployed",
             {"mode": "k8s", "url": url or "pending load balancer", "image": image})
    return {"ok": True, "mode": "k8s", "image": image, "url": url,
            "routing": "shared ingress" if use_ingress else "dedicated load balancer",
            "note": "" if url else "the load balancer is still being assigned an IP",
            "spec": spec}


def _generated_dockerfile(spec: dict) -> str:
    if spec["kind"] == "node":
        return ("FROM node:20-alpine\nWORKDIR /app\nCOPY package*.json ./\n"
                "RUN npm ci --omit=dev || npm install --omit=dev\nCOPY . .\n"
                "ENV PORT=8080\nEXPOSE 8080\nCMD [\"npm\", \"start\"]\n")
    return ("FROM python:3.11-slim\nWORKDIR /app\nCOPY requirements.txt* ./\n"
            "RUN pip install --no-cache-dir -r requirements.txt || true\nCOPY . .\n"
            "ENV PORT=8080\nEXPOSE 8080\n"
            f"CMD [\"/bin/sh\", \"-c\", \"{spec['run'].replace('$PORT', '8080')}\"]\n")


def status(project_id: int) -> dict[str, Any]:
    """What is deployed right now, and what would happen if you deployed."""
    root = workdir(project_id)
    spec = detect(root) if root.exists() else None
    r = RUNNING.get(project_id) or _adopt(project_id)
    live = None
    if r:
        if r["proc"].poll() is None:
            live = {"mode": "local", "url": f"http://localhost:{r['port']}",
                    "port": r["port"], "uptime": int(time.time() - r["started"]),
                    "kind": r["spec"]["kind"]}
        else:
            RUNNING.pop(project_id, None)   # it died on its own
            pid_file(project_id).unlink(missing_ok=True)
    log = ""
    if log_path(project_id).exists():
        log = log_path(project_id).read_text()[-6000:]
    return {"live": live, "spec": spec, "log": log,
            "k8s_available": bool(shutil.which("kubectl")),
            "docker_available": bool(shutil.which("docker")),
            "default_mode": "k8s" if config.LAUNCHER == "k8s" else "local"}
