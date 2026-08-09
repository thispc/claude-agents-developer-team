"""A project with no GitHub repo is a first-class project.

The failure these cover is the worst kind the platform has had: it was silent and
it was destructive. A project created without a repo ran, spent tokens and built a
working application into `workspaces/task-<id>-a<n>/repo`. Every route that could
have handed it back refused — "no GitHub repo attached to this project" — there was
no download anywhere, and `prune_workspaces` deleted the directory on a later boot
with no event and no log line. The owner's weather app existed in exactly one place
and the platform was counting down to removing it.

So these assert the whole path end to end, offline: a task delivers, the work is
preserved somewhere the pruner does not reach, the Files tab lists it, the download
returns a zip whose contents match the tree, and the with-repo path is untouched.
"""

import io
import json
import zipfile
from pathlib import Path

import pytest

from conftest import make_project, make_task
from app import artifacts, config, db, deliverables, launcher, manager, scheduler


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    """Workspaces and deliverables on their own temp roots, as they are on the
    volume in production — deliberately NOT inside one another."""
    ws = tmp_path / "workspaces"
    dl = tmp_path / "deliverables"
    ws.mkdir()
    monkeypatch.setattr(config, "WORKSPACES_DIR", ws)
    monkeypatch.setattr(config, "DELIVERABLES_DIR", dl)
    return ws, dl


def _workspace(ws: Path, task_id: int, files: dict, attempt: int = 1) -> Path:
    """A worker's checkout as it stands when the task reports — including the
    directories the copy is supposed to leave behind."""
    repo = ws / f"task-{task_id}-a{attempt}" / "repo"
    for name, content in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / "node_modules" / "left-pad").mkdir(parents=True, exist_ok=True)
    (repo / "node_modules" / "left-pad" / "index.js").write_text("module.exports=1")
    return repo


APP = {"index.html": "<!doctype html><title>weather</title>",
       "src/main.js": "export const go = () => 1;\n",
       "README.md": "# Weather\n"}


# --- 1. the work survives the task that made it ------------------------------

@pytest.mark.asyncio
async def test_a_delivered_task_with_no_remote_is_preserved(fresh_db, dirs):
    ws, dl = dirs
    pid = make_project(name="weather", repo="")
    tid = make_task(pid, status="pushed")
    _workspace(ws, tid, APP)

    await scheduler._auto_open_pr(db.get_project(pid), db.get_task(tid))

    kept = dl / str(pid) / f"task-{tid}"
    assert (kept / "index.html").exists()
    assert (kept / "src" / "main.js").exists()
    # The copy is deploy.sync_from_workspace's, so it drops what a deliverable has
    # no business carrying — the history and the dependency tree.
    assert not (kept / ".git").exists()
    assert not (kept / "node_modules").exists()
    # ...and the task still goes to review, exactly as it did before.
    assert db.get_task(tid)["status"] == "review"


@pytest.mark.asyncio
async def test_preserving_says_so_on_the_feed(fresh_db, dirs):
    ws, _ = dirs
    pid = make_project(repo="")
    tid = make_task(pid, status="pushed")
    _workspace(ws, tid, APP)
    await scheduler._auto_open_pr(db.get_project(pid), db.get_task(tid))
    kinds = [e["kind"] for e in db.list_events(pid)]
    assert "work_preserved" in kinds


@pytest.mark.asyncio
async def test_a_missing_workspace_is_reported_not_swallowed(fresh_db, dirs):
    """Losing the snapshot must not also lose the task: the report is still worth
    having, and the boss is told the copy did not happen."""
    pid = make_project(repo="")
    tid = make_task(pid, status="pushed")            # no workspace on disk at all
    await scheduler._auto_open_pr(db.get_project(pid), db.get_task(tid))
    kinds = [e["kind"] for e in db.list_events(pid)]
    assert "preserve_failed" in kinds
    assert db.get_task(tid)["status"] == "review"


@pytest.mark.asyncio
async def test_the_newest_delivery_is_the_projects_deliverable(fresh_db, dirs):
    """Each task has its own tree when there is no repo to share, so they are kept
    apart — and 'the project's code' is the most recent one, named."""
    ws, dl = dirs
    pid = make_project(repo="")
    first = make_task(pid, title="scaffold", status="pushed")
    _workspace(ws, first, {"index.html": "v1"})
    await scheduler._auto_open_pr(db.get_project(pid), db.get_task(first))
    second = make_task(pid, title="restyle", status="pushed")
    _workspace(ws, second, {"index.html": "v2", "style.css": "body{}"})
    await scheduler._auto_open_pr(db.get_project(pid), db.get_task(second))

    assert len(deliverables.manifest(pid)) == 2
    latest = deliverables.latest(pid)
    assert latest["task_id"] == second
    assert latest["title"] == "restyle"
    assert (Path(latest["path"]) / "index.html").read_text() == "v2"
    # The earlier one is still there — a later delivery does not overwrite it.
    assert (dl / str(pid) / f"task-{first}" / "index.html").read_text() == "v1"


@pytest.mark.asyncio
async def test_boot_preserves_work_delivered_before_this_existed(fresh_db, dirs):
    """Everything already on the machine was delivered by a path with nowhere to
    put it, and no hook on the delivery path can ever reach those tasks again.
    So boot applies the same rule once, before the pruner runs."""
    ws, dl = dirs
    pid = make_project(status="done", repo="")
    old = make_task(pid, status="done")
    _workspace(ws, old, APP)
    with_repo = make_project(repo="them/theirs")
    theirs = make_task(with_repo, status="done")
    _workspace(ws, theirs, APP)

    kept = await deliverables.backfill()

    assert len(kept) == 1
    assert (dl / str(pid) / f"task-{old}" / "index.html").exists()
    # ...and never for a project with a remote: there the branch is the record,
    # and copying would be new behaviour on a path that must not change.
    assert not (dl / str(with_repo)).exists()
    assert await deliverables.backfill() == []      # idempotent


@pytest.mark.asyncio
async def test_the_backfill_runs_before_the_pruner(fresh_db):
    """Copy first, delete second. The other order is the bug this exists to close."""
    src = (Path(__file__).resolve().parent.parent / "conductor" / "app" / "main.py").read_text()
    assert src.index("deliverables.backfill()") < src.index("launcher.prune_workspaces()")


@pytest.mark.asyncio
async def test_a_contest_preserves_the_winner_not_whoever_wrote_last(fresh_db, dirs):
    """Rivals finish seconds apart, so 'newest' is a coin toss between the work the
    manager chose and the work it threw away."""
    ws, dl = dirs
    pid = make_project(repo="")
    tid = make_task(pid, status="pushed")
    _workspace(ws, tid, {"index.html": "the winner"}, attempt=1)
    (ws / f"task-{tid}-a1").rename(ws / f"task-{tid}-a1-c1")
    _workspace(ws, tid, {"index.html": "the loser"}, attempt=1)   # written LAST
    (ws / f"task-{tid}-a1").rename(ws / f"task-{tid}-a1-c2")
    won = db.create_contender(tid, 1, f"task/{tid}-a1-c1", "m")
    db.create_contender(tid, 2, f"task/{tid}-a1-c2", "m")
    db.update_contender(won, status="won")

    await scheduler._auto_open_pr(db.get_project(pid), db.get_task(tid))
    assert (dl / str(pid) / f"task-{tid}" / "index.html").read_text() == "the winner"


def test_a_lost_index_does_not_lose_the_work(fresh_db, dirs):
    """The directories are the record. index.json is a convenience, so deleting it
    costs metadata and never the deliverable."""
    _ws, dl = dirs
    pid = make_project(repo="")
    tid = make_task(pid, title="the app")
    d = dl / str(pid) / f"task-{tid}"
    d.mkdir(parents=True)
    (d / "index.html").write_text("hello")

    rows = deliverables.manifest(pid)
    assert [r["task_id"] for r in rows] == [tid]
    assert rows[0]["title"] == "the app"        # recovered from the task row
    assert deliverables.list_files(pid)[0]["path"] == "index.html"


# --- 2. the pruner ------------------------------------------------------------

def test_prune_leaves_a_delivered_task_of_a_live_project_alone(fresh_db, dirs):
    """The exact deletion that was scheduled to take the owner's weather app: the
    task is 'done', so no live check protects it, the project is still going, and
    twelve later workspaces have pushed it out of the keep-window."""
    ws, _dl = dirs
    pid = make_project(status="running", repo="")
    delivered = make_task(pid, status="done")
    (ws / f"task-{delivered}-a1" / "repo").mkdir(parents=True)
    churn = make_task(pid, status="failed")
    for i in range(12):                     # later work, so the window is blown
        (ws / f"task-{churn}-a{i}").mkdir()

    launcher.prune_workspaces(keep=2)
    assert (ws / f"task-{delivered}-a1").exists()
    assert len(list(ws.iterdir())) == 3, "the churn should still have been pruned"


def test_prune_protects_the_latest_attempt_and_prunes_the_rest(fresh_db, dirs):
    ws, _dl = dirs
    pid = make_project(status="running", repo="")
    tid = make_task(pid, status="done")
    for a in range(1, 6):
        (ws / f"task-{tid}-a{a}").mkdir()
    launcher.prune_workspaces(keep=0)
    left = sorted(p.name for p in ws.iterdir())
    assert left == [f"task-{tid}-a5"]


def test_the_budget_does_not_reach_protected_work(fresh_db, dirs):
    """Reclaiming space is worth less than the only copy of an application."""
    ws, _dl = dirs
    pid = make_project(status="running", repo="")
    tid = make_task(pid, status="done")
    d = ws / f"task-{tid}-a1"
    d.mkdir()
    (d / "blob").write_bytes(b"x" * 50 * 1024)
    launcher.prune_workspaces(keep=0, budget=1)
    assert d.exists()


def test_a_finished_project_is_prunable_again(fresh_db, dirs):
    """Protection that never lifts means a pruner that slowly stops working."""
    ws, _dl = dirs
    pid = make_project(status="done", repo="")
    tid = make_task(pid, status="done")
    for a in range(1, 4):
        (ws / f"task-{tid}-a{a}").mkdir()
    assert launcher.prune_workspaces(keep=0) == 3
    assert list(ws.iterdir()) == []


def test_deleting_a_workspace_is_announced_with_its_name(fresh_db, dirs, monkeypatch):
    """It ran on every boot, deleted whole applications, and said nothing. A count
    tells you a number; the names tell you which run's evidence you no longer have."""
    ws, _dl = dirs
    said = []
    monkeypatch.setattr(launcher.bus, "emit",
                        lambda *a, **k: said.append((a[3], a[4])) or {})
    pid = make_project(status="done", repo="")
    tid = make_task(pid, status="done")
    (ws / f"task-{tid}-a1").mkdir()
    launcher.prune_workspaces(keep=0)

    pruned = [payload for kind, payload in said if kind == "workspaces_pruned"]
    assert pruned, "the pruner deleted a workspace and said nothing"
    assert pruned[0]["count"] == 1
    assert f"task-{tid}-a1" in [w["workspace"] for w in pruned[0]["workspaces"]]
    assert f"task-{tid}-a1" in pruned[0]["detail"]


def test_a_run_that_deletes_nothing_still_says_nothing(fresh_db, dirs, monkeypatch):
    """An alarm that fires when everything is fine is an alarm nobody reads."""
    ws, _dl = dirs
    said = []
    monkeypatch.setattr(launcher.bus, "emit", lambda *a, **k: said.append(a) or {})
    pid = make_project(status="done", repo="")
    tid = make_task(pid, status="done")
    (ws / f"task-{tid}-a1").mkdir()
    launcher.prune_workspaces(keep=8, budget=1024 * 1024)
    assert said == []


# --- 3. reaching the work through the product ---------------------------------

@pytest.fixture()
def delivered(root_client, dirs, monkeypatch):
    """A no-repo project whose one task has delivered, through the real path."""
    import asyncio
    ws, _dl = dirs
    pid = make_project(owner_id=1, name="weather app", repo="")
    tid = make_task(pid, title="scaffold the app", status="pushed")
    _workspace(ws, tid, APP)
    asyncio.run(scheduler._auto_open_pr(db.get_project(pid), db.get_task(tid)))
    return root_client, pid, tid


def test_the_files_tab_lists_the_deliverable(delivered):
    client, pid, tid = delivered
    r = client.get(f"/api/projects/{pid}/files")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "deliverable"
    assert sorted(f["path"] for f in body["files"]) == \
        ["README.md", "index.html", "src/main.js"]
    # It says whose work it is rather than presenting itself as "the repo".
    assert body["delivered_by"]["task_id"] == tid
    assert body["download_url"] == f"/api/projects/{pid}/download"
    # ...and never the excluded trees, which is what made a 33 MB workspace a
    # 4 KB deliverable.
    assert not any(".git" in f["path"] or "node_modules" in f["path"]
                   for f in body["files"])


def test_one_file_opens_without_a_repo(delivered):
    client, pid, _tid = delivered
    r = client.get(f"/api/projects/{pid}/file", params={"path": "README.md"})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "# Weather\n"


def test_a_path_cannot_climb_out_of_the_deliverable(delivered):
    client, pid, _tid = delivered
    for path in ("../../devteam.db", "src/../../../etc/passwd"):
        assert client.get(f"/api/projects/{pid}/file",
                          params={"path": path}).status_code == 400


def test_the_download_is_a_zip_whose_contents_match(delivered):
    client, pid, _tid = delivered
    r = client.get(f"/api/projects/{pid}/download")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert ".zip" in r.headers.get("content-disposition", "")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.testzip() is None
    assert sorted(z.namelist()) == ["README.md", "index.html", "src/main.js"]
    assert z.read("index.html").decode() == APP["index.html"]
    assert z.read("src/main.js").decode() == APP["src/main.js"]


def test_the_download_is_root_gated(delivered, make_user):
    client, pid, _tid = delivered
    _uid, other = make_user("nosy")
    assert other.get(f"/api/projects/{pid}/download").status_code in (403, 404)
    assert other.get(f"/api/projects/{pid}/files").status_code in (403, 404)


def test_downloading_nothing_says_so(root_client, dirs):
    pid = make_project(owner_id=1, repo="")
    r = root_client.get(f"/api/projects/{pid}/download")
    assert r.status_code == 404
    assert "delivered" in r.json()["detail"]


def test_an_oversized_deliverable_is_refused_with_a_sentence(delivered, monkeypatch):
    client, pid, _tid = delivered
    monkeypatch.setattr(deliverables, "MAX_ZIP_BYTES", 4)
    r = client.get(f"/api/projects/{pid}/download")
    assert r.status_code == 413
    assert "limit" in r.json()["detail"]


def test_the_artifacts_payload_offers_the_download(delivered):
    client, pid, tid = delivered
    a = client.get(f"/api/projects/{pid}/artifacts").json()
    assert a["has_remote"] is False
    assert a["download_url"] == f"/api/projects/{pid}/download"
    assert a["deliverable"]["task_id"] == tid
    assert a["deliverable"]["files"] == 3


@pytest.mark.asyncio
async def test_a_frozen_sprint_records_the_files_without_github(fresh_db, dirs):
    """The file list is the part of a snapshot that cannot be reconstructed later.
    Gating it on GitHub meant a no-repo project froze a sprint that recorded no
    output at all."""
    _ws, dl = dirs
    pid = make_project(repo="")
    tid = make_task(pid, status="done")
    d = dl / str(pid) / f"task-{tid}"
    d.mkdir(parents=True)
    (d / "index.html").write_text("hi")
    snap = await artifacts.capture(pid, 1)
    assert snap["facts"]["files_source"] == "deliverable"
    assert [f["path"] for f in snap["facts"]["files"]] == ["index.html"]


@pytest.mark.asyncio
async def test_the_preview_builds_from_the_deliverable(fresh_db, dirs, tmp_path,
                                                       monkeypatch):
    from app import preview
    _ws, dl = dirs
    monkeypatch.setattr(preview, "PREVIEW_DIR", tmp_path / "previews")
    pid = make_project(repo="")
    tid = make_task(pid, status="done")
    d = dl / str(pid) / f"task-{tid}"
    d.mkdir(parents=True)
    (d / "index.html").write_text("<h1>hi</h1>")

    ok, note = await preview.sync(pid)
    assert ok, note
    assert preview.preview_root(pid) is not None
    assert preview.synced_at(pid)          # and it knows how fresh it is, with no .git


@pytest.mark.asyncio
async def test_a_project_with_nothing_delivered_cannot_be_previewed_yet(fresh_db, dirs,
                                                                        tmp_path,
                                                                        monkeypatch):
    from app import preview
    monkeypatch.setattr(preview, "PREVIEW_DIR", tmp_path / "previews")
    pid = make_project(repo="")
    ok, note = await preview.sync(pid)
    assert not ok
    assert "delivered" in note


# --- 4. nothing promises a pull request that cannot exist ---------------------

def test_the_manager_is_told_there_is_no_remote_only_when_true(fresh_db):
    without = manager.delivery_brief({"repo": ""})
    assert "NO REMOTE REPOSITORY" in without
    assert "accept_task" in without
    with_repo = manager.delivery_brief({"repo": "someone/thing"})
    assert with_repo == ""
    # ...and blank-but-present is the same as absent, which is how the row reads.
    assert manager.delivery_brief({"repo": "   "}) != ""


def test_a_project_without_a_repo_needs_no_github_token(client, make_user, monkeypatch):
    """'github shouldn't be a requirement unless PRs are required.' This gate used
    to refuse every project from a user with no GitHub token."""
    from app import auth, config as config_mod
    monkeypatch.setattr(config_mod, "AUTH_CONFIGURED", True)
    uid, c2 = make_user("norepo")
    auth.save_settings(uid, {"anthropic_api_key": "sk-ant-test-not-a-real-key"})
    r = c2.post("/api/projects", json={"name": "no remote", "brief": "build a thing"})
    assert r.status_code == 200, r.text
    assert db.get_project(r.json()["id"])["repo"] == ""


def test_asking_for_a_repo_still_needs_a_token(client, make_user, monkeypatch):
    """The requirement did not go away; it moved to where it is true."""
    from app import auth, config as config_mod
    monkeypatch.setattr(config_mod, "AUTH_CONFIGURED", True)
    uid, c2 = make_user("wantsrepo")
    auth.save_settings(uid, {"anthropic_api_key": "sk-ant-test-not-a-real-key"})
    r = c2.post("/api/projects", json={"name": "with remote", "brief": "b",
                                       "repo": "them/theirs"})
    assert r.status_code == 400
    assert "GitHub token" in r.json()["detail"]


def test_the_dashboard_offers_a_download_and_no_phantom_prs(fresh_db):
    from conftest import dashboard_js
    js = dashboard_js()
    assert "downloadBtn" in js
    assert "download_url" in js
    # The pull-request list is rendered only for a project that can have one.
    assert "a.has_remote" in js


def test_the_worker_is_told_not_to_push_when_there_is_no_remote():
    src = (Path(__file__).resolve().parent.parent / "worker" / "worker.py").read_text()
    assert "There is no remote for this project" in src
    assert "no remote configured; changes remain in workspace" in src


# --- 5. the with-repo path is untouched ---------------------------------------

@pytest.mark.asyncio
async def test_a_project_with_a_repo_still_opens_a_pull_request(fresh_db, dirs,
                                                                monkeypatch):
    """Pinned. Everything above is additive, and the way to prove that is to run
    the old path and watch it do exactly what it did: a PR, no snapshot, no new
    directory."""
    _ws, dl = dirs
    from app import github_client
    monkeypatch.setattr(github_client, "enabled", lambda repo, token=None: bool(repo))
    monkeypatch.setattr(github_client, "find_pr_for_branch",
                        _async(None))
    monkeypatch.setattr(github_client, "default_branch", _async("main"))
    monkeypatch.setattr(github_client, "create_pr", _async(41))

    pid = make_project(repo="them/theirs")
    tid = make_task(pid, status="pushed")
    await scheduler._auto_open_pr(db.get_project(pid), db.get_task(tid))

    t = db.get_task(tid)
    assert t["pr_number"] == 41 and t["status"] == "review"
    assert not (dl / str(pid)).exists(), "the with-repo path wrote a deliverable"
    kinds = [e["kind"] for e in db.list_events(pid)]
    assert "pr_opened" in kinds and "work_preserved" not in kinds


def test_a_repo_backed_files_tab_still_reads_the_repo(root_client, monkeypatch, dirs):
    from app import github_client
    monkeypatch.setattr(github_client, "enabled", lambda repo, token=None: bool(repo))

    async def _tree(repo, ref="", token=""):
        return [{"path": "app/main.py", "size": 12}]
    monkeypatch.setattr(github_client, "list_tree", _tree)

    pid = make_project(owner_id=1, repo="them/theirs")
    body = root_client.get(f"/api/projects/{pid}/files").json()
    assert body["repo"] == "them/theirs" and body["source"] == "repo"
    assert body["files"] == [{"path": "app/main.py", "size": 12, "kind": "code"}]


def _async(value):
    async def _f(*a, **k):
        return value
    return _f
