"""What a project built, when there is no repository to read it back out of.

GitHub was never a feature of this platform; it was load-bearing plumbing that
every retrieval route happened to be written against. A project created without a
repo still ran, still spent tokens and still produced a working application — and
then every way of reaching that application refused, because each of them starts
by asking for `project.repo`. The Files tab, the preview, the deploy, the sprint
artifact's file list: all of them answered "no GitHub repo attached". The work
existed in exactly one place, `workspaces/task-<id>-a<n>/repo`, and eight boots
later the pruner deleted it without a word.

So: **when a task delivers and there is no remote, the workspace is copied
somewhere durable, and that copy is the deliverable.** Three decisions worth
knowing.

**It lives in `deliverables/<project_id>/task-<task_id>/`, not in previews/.**
`previews/` is a *derived* directory — preview.sync clones and npm-builds into it
and will happily overwrite it — and a thing that must survive cannot live where
another process rebuilds. `deliverables/` sits beside `workspaces/` and
`deployments/`, which means it is on the volume in a container, and nothing but
this module writes there.

**One directory per task, never one per project.** With no remote, each worker
starts from `git init` in its own empty workspace, so tasks do not share a tree
the way a repo makes them share one. Folding every task into one project
directory would let the last delivery silently overwrite the previous one, which
is the same data loss with a tidier path. The project-level answer — "show me
what this project produced" — is the NEWEST snapshot, and it says which task it
came from rather than pretending to be the whole project.

**The directories are the record; the index is a convenience.** `index.json`
carries the title, role and time of each snapshot, but every read falls back to
scanning the directories, so a lost or corrupt index costs metadata and never the
work itself.

The copy is `deploy.sync_from_workspace`, unchanged and shared: it already copies
a working tree as it stands, uncommitted work included, and needs no git.
"""

import json
import time
import zipfile
from pathlib import Path
from typing import Any

from . import config, db

# Never zipped, whatever is on disk. sandbox.snapshot drops most of these on the
# way in, but a download must be safe against a snapshot made some other way —
# and `.git` is deliberate here: the archive is the work, not its history.
ZIP_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
            ".pytest_cache", ".DS_Store", ".next"}

# A download is a synchronous request holding a worker; an accidental 4 GB tree
# (a model that committed a dataset) must be refused with a sentence rather than
# quietly consuming the process. Measured before compression, on purpose: what
# it costs us to read is what matters, not what it costs the browser to receive.
MAX_ZIP_BYTES = int(config._env("DELIVERABLE_MAX_ZIP_BYTES", str(200 * 1024**2)))


def project_dir(project_id: int) -> Path:
    return config.DELIVERABLES_DIR / str(project_id)


def task_dir(project_id: int, task_id: int) -> Path:
    return project_dir(project_id) / f"task-{task_id}"


def _index_file(project_id: int) -> Path:
    return project_dir(project_id) / "index.json"


def _read_index(project_id: int) -> dict[str, dict]:
    try:
        raw = json.loads(_index_file(project_id).read_text())
        return {str(k): v for k, v in (raw or {}).items() if isinstance(v, dict)}
    except Exception:
        return {}


def _write_index(project_id: int, index: dict[str, dict]) -> None:
    f = _index_file(project_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(index, indent=2, sort_keys=True))


def newest_workspace(task_id: int) -> str:
    """The workspace directory holding the work this task delivered, or ''.

    A task can have several — one per attempt, plus one per rival in a contest —
    and the newest is normally the one that produced the report. A CONTEST is the
    exception worth handling: its rivals finish seconds apart, so "newest" would
    preserve whichever of them happened to write last, which is a coin toss
    between the work the manager chose and the work it threw away.
    """
    base = config.WORKSPACES_DIR
    if not base.exists():
        return ""
    mine = [d for d in base.iterdir()
            if d.is_dir() and (d.name == f"task-{task_id}"
                               or d.name.startswith(f"task-{task_id}-"))]
    if not mine:
        return ""
    try:
        won = [c for c in db.list_contenders(task_id) if c["status"] == "won"]
    except Exception:
        won = []
    if won:
        # The launcher labels a rival's workspace `-c<idx>` (LocalLauncher.launch).
        winners = [d for d in mine if d.name.endswith(f"-c{won[0]['idx']}")]
        mine = winners or mine
    return max(mine, key=lambda d: (d.stat().st_mtime, d.name)).name


async def preserve(project: dict, task: dict) -> tuple[bool, str]:
    """Copy a delivered task's workspace where it will still be there tomorrow.

    Called on the no-remote delivery path. Returns (ok, note); a failure is
    reported rather than raised, because losing the snapshot must not also lose
    the task — the report is still worth having.
    """
    from . import deploy
    project_id, task_id = project["id"], task["id"]
    workspace = newest_workspace(task_id)
    if not workspace:
        return False, f"no workspace left on disk for task {task_id}"
    dest = task_dir(project_id, task_id)
    ok, note = await deploy.sync_from_workspace(project_id, workspace, dest=dest)
    if not ok:
        return False, note
    index = _read_index(project_id)
    index[str(task_id)] = {
        "task_id": task_id,
        "seq": task.get("seq"),
        "title": task.get("title", ""),
        "role": task.get("role", ""),
        "workspace": workspace,
        "taken_at": time.time(),
        "files": len(_walk(dest)),
        "bytes": size_of(dest),
    }
    _write_index(project_id, index)
    return True, f"preserved {workspace} as this project's deliverable for task {task_id}"


async def backfill() -> list[str]:
    """Preserve delivered work that predates the delivery hook, at boot.

    Everything already on this machine when this module arrived was delivered by
    a path that had nowhere to put it — the owner's weather app among it, five
    delivered tasks sitting in `workspaces/` with the pruner counting down. A hook
    on the delivery path cannot reach any of them: those tasks are already 'done'
    and will never pass through it again.

    So the same rule is applied once at startup, BEFORE the pruner runs: any
    delivered task of a project with no remote, whose workspace is still on disk
    and whose deliverable is missing, is preserved now. Idempotent and cheap —
    after the first boot it is a directory check per delivered task and nothing
    else — and deliberately silent about tasks whose workspace is already gone,
    because there is nothing left to say about those.

    Restricted to projects with NO REPO. With a remote the branch is the record,
    and copying there would be new behaviour on a path that must not change.
    """
    done: list[str] = []
    for p in db.list_projects():
        if (p.get("repo") or "").strip():
            continue
        for t in db.list_tasks(p["id"]):
            if t["status"] not in ("done", "review", "pushed"):
                continue
            if task_dir(p["id"], t["id"]).is_dir():
                continue
            if not newest_workspace(t["id"]):
                continue
            ok, note = await preserve(p, t)
            if ok:
                done.append(f"project {p['id']} task {t['id']}")
    return done


def manifest(project_id: int) -> list[dict[str, Any]]:
    """Every preserved deliverable of this project, newest first.

    Built from the DIRECTORIES and only decorated from the index, so a project
    whose index.json was lost still lists everything it produced.
    """
    root = project_dir(project_id)
    if not root.exists():
        return []
    index = _read_index(project_id)
    out = []
    for d in root.iterdir():
        if not d.is_dir() or not d.name.startswith("task-"):
            continue
        try:
            task_id = int(d.name.removeprefix("task-"))
        except ValueError:
            continue
        rec = dict(index.get(str(task_id)) or {})
        rec.setdefault("task_id", task_id)
        rec.setdefault("taken_at", d.stat().st_mtime)
        rec["path"] = str(d)
        if not rec.get("title"):
            t = db.get_task(task_id) or {}
            rec["title"] = t.get("title", "")
            rec["role"] = rec.get("role") or t.get("role", "")
            rec["seq"] = rec.get("seq") or t.get("seq")
        out.append(rec)
    out.sort(key=lambda r: r.get("taken_at") or 0, reverse=True)
    return out


def latest(project_id: int) -> dict[str, Any] | None:
    """The project's current deliverable: the most recently preserved task.

    Not a merge of every task's tree. A union would be a directory nobody built
    and nobody could reproduce; this is a real thing that really ran, and it says
    whose work it is.
    """
    rows = manifest(project_id)
    return rows[0] if rows else None


def root(project_id: int) -> Path | None:
    """The directory to list, serve or zip for this project, or None."""
    row = latest(project_id)
    if not row:
        return None
    p = Path(row["path"])
    return p if p.is_dir() else None


def _walk(base: Path) -> list[Path]:
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        if ZIP_SKIP & set(p.relative_to(base).parts):
            continue
        out.append(p)
    return out


def size_of(base: Path) -> int:
    total = 0
    for p in _walk(base):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def _kind(path: str) -> str:
    """What a file IS to a reader — the same grouping the repo-backed Files tab
    uses, so the tab reads identically with or without a remote."""
    low = path.lower()
    if low.endswith((".md", ".txt", ".rst")):
        return "doc"
    if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico")):
        return "image"
    if "test" in low:
        return "test"
    return "code"


def list_files(project_id: int) -> list[dict[str, Any]]:
    """The deliverable's files, in the shape github_client.list_tree returns."""
    base = root(project_id)
    if base is None:
        return []
    return [{"path": str(p.relative_to(base)), "size": p.stat().st_size,
             "kind": _kind(str(p))}
            for p in _walk(base)]


# Reading a file out of the deliverable is reading agent-written content off the
# operator's disk, so the path is resolved and confined rather than trusted.
MAX_TEXT_BYTES = 400_000


def read_text(project_id: int, path: str) -> str:
    base = root(project_id)
    if base is None:
        raise FileNotFoundError("nothing has been delivered for this project yet")
    target = (base / path).resolve()
    if not str(target).startswith(str(base.resolve()) + "/") or not target.is_file():
        raise FileNotFoundError("no such file in this project's deliverable")
    if target.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("too large to show")
    try:
        return target.read_text()
    except UnicodeDecodeError:
        raise ValueError("not a text file")


def write_zip(project_id: int, out: Path) -> tuple[bool, str, int]:
    """Zip the current deliverable into `out`. Returns (ok, note_or_error, files)."""
    base = root(project_id)
    if base is None:
        return False, "nothing has been delivered for this project yet", 0
    files = _walk(base)
    if not files:
        return False, "this project's deliverable is empty", 0
    total = sum(f.stat().st_size for f in files)
    if total > MAX_ZIP_BYTES:
        return False, (f"this deliverable is {total // 1048576} MB, over the "
                       f"{MAX_ZIP_BYTES // 1048576} MB download limit — open it from "
                       f"the Files tab instead"), 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, str(f.relative_to(base)))
    return True, f"{len(files)} file(s)", len(files)


def archive_name(project: dict, row: dict | None) -> str:
    """A filename someone can find again on their desktop."""
    import re
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", (project.get("name") or "project")).strip("-")
    seq = (row or {}).get("seq")
    tail = f"-task-{seq}" if seq else ""
    return f"{name or 'project'}{tail}.zip"


def protected_task_ids() -> set[int]:
    """Tasks whose workspace the pruner must not take.

    A task that DELIVERED on a project that is still going is the case the old
    rule missed: it is not 'live' by any status check, its work may exist nowhere
    else, and eight boots later it was gone. Projects that are done or cancelled
    are excluded deliberately — their work has already been preserved or it never
    will be, and protecting them forever would mean the pruner slowly stops being
    able to reclaim anything.
    """
    try:
        rows = db._rows(
            "SELECT t.id AS id FROM tasks t JOIN projects p ON p.id = t.project_id "
            "WHERE t.status IN ('done','pushed','review') "
            "AND p.status NOT IN ('done','cancelled')")
    except Exception:
        return set()
    return {int(r["id"]) for r in rows}
