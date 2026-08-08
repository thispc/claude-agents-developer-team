"""The notify service — getting word out when nobody is looking at the dashboard.

"Unattended" was only half true: the platform kept working without a human, and
told them nothing. Every fault — a crashed manager session, a provider returning
500s, a JavaScript error in the dashboard — landed in the events table, which is
only visible to someone already watching the feed. Walk away for a night and the
first you hear of a problem is that nothing got done.

The channel is GitHub issues, chosen because it costs nothing to add: the token
already exists, and GitHub already pushes to your email and phone. Building an
email path would mean another credential, another service, another thing to be
down.

Two rules make it survivable rather than a spam machine, and they are the whole
reason this is a service rather than a function:

- **One issue per distinct fault.** A crash loop produces the same fingerprint a
  thousand times; it should produce one issue with a count, not a thousand
  issues. That is the difference between a notification and a denial of service
  against your own inbox.
- **A ceiling per hour.** If something goes wrong in a way we did not anticipate,
  the failure mode is silence, not an unbounded write loop against the GitHub API
  with a token that can also push code.

Both of those are STATE, and until P2 that state was two kv blobs in the
conductor's database, each rewritten whole (`notify_sent`, `notify_seen:*`). Two
processes filing at once and one of them forgets it filed — on the exact counter
whose job is to stop a flood. Here they are two ordinary tables: one row per
distinct fault, one row per issue actually filed. Nothing to read-modify-write.

THE CREDENTIAL. This is the first service beyond the conductor to hold one:
`GITHUB_TOKEN` arrives in its env from `tools/gen_fleet.py` because the GitHub
call itself moved here, and a notifier that has to ask another process to speak
for it is just the old coupling with extra latency. The env stays otherwise
minimal — no model keys, no database but its own — and `SERVICE_CONTRACT.md`
rule 4 names this as the one documented exception it makes.

WHAT STAYED IN THE CONDUCTOR. The repo to file against is derived from the git
remote, which is conductor knowledge, so it RIDES each request — the same shape
the knowledge service's embedding key uses. And `sprint_digest` stays conductor-
side: it formats projects and tasks out of the conductor's own database and then
sends the finished text through the generic `POST /issue`.
"""

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


class StrictBody(BaseModel):
    """Strict validation: the contract says integer, so "5" is a 422, not a
    quiet coercion — lax mode is how a spec and a service drift apart."""
    model_config = ConfigDict(strict=True)


def _json_int(v):
    """JSON Schema's `integer` means "a number with zero fractional part" — 24.0
    qualifies — while Python's strict mode wants the int type itself."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


JsonInt = Annotated[int, BeforeValidator(_json_int)]

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))       # so `uvicorn app:app` works from any cwd
import helpers                      # vendored per service — never imported across services

SERVICE = os.environ.get("SERVICE_NAME", HERE.name)
SPEC = HERE / "openapi.json"
LEGACY_DB_PATH = Path(os.environ.get("LEGACY_DB_PATH", "devteam.db"))

CONDUCTOR_URL = (os.environ.get("CONDUCTOR_URL") or "").strip().rstrip("/")

MAX_PER_HOUR = int(os.environ.get("NOTIFY_MAX_PER_HOUR") or "6")
ENABLED = (os.environ.get("NOTIFY_GITHUB") or "1") != "0"

# The git host. Same three shapes the conductor supports (GitHub, GitHub
# Enterprise Server at /api/v3, Gitea at /api/v1) — a different host is
# configuration, not a provider.
GIT_API = (os.environ.get("GIT_API") or "https://api.github.com").rstrip("/")
GIT_PROVIDER = (os.environ.get("GIT_PROVIDER") or "github").strip().lower()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or ""

N_SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_seen (
    fp      TEXT PRIMARY KEY,               -- the fingerprint: what makes two faults 'the same'
    kind    TEXT NOT NULL DEFAULT '',
    head    TEXT NOT NULL DEFAULT '',       -- the first line, for a readable status page
    count   INTEGER NOT NULL DEFAULT 1,
    first   REAL NOT NULL,
    last    REAL NOT NULL,
    issue   INTEGER                          -- NULL until one is actually filed
);
CREATE TABLE IF NOT EXISTS notify_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL                         -- one row per issue filed; the hourly ceiling counts them
);
CREATE INDEX IF NOT EXISTS idx_notify_sent_ts ON notify_sent(ts);
"""


# --- this service's own store -------------------------------------------------

def _execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    con = helpers.db()
    cur = con.execute(sql, params)
    con.commit()
    return cur


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in helpers.db().execute(sql, params).fetchall()]


def init_store() -> None:
    con = helpers.db()          # helpers.db() already sets row_factory
    con.executescript(N_SCHEMA)
    con.commit()


def backfill_from_legacy() -> int:
    """First boot only: carry the conductor's dedup memory across the seam.

    Without it the extraction itself becomes a notification storm — every fault
    the platform had already filed an issue for would look new again on the first
    boot after the cutover, which is precisely the failure this module exists to
    prevent. The hourly counter is deliberately NOT copied: it is a
    rate-limiter's sliding window, it empties within the hour anyway, and
    starting it at zero errs toward being able to tell you something.
    """
    if helpers.kv_get("backfilled_from"):
        return 0
    if _rows("SELECT COUNT(*) AS n FROM notify_seen")[0]["n"]:
        return 0
    if not LEGACY_DB_PATH.exists():
        return 0
    con = helpers.db()
    copied = 0
    try:
        con.execute("ATTACH DATABASE ? AS legacy", (f"file:{LEGACY_DB_PATH}?mode=ro",))
        try:
            for row in con.execute(
                    "SELECT k, v FROM legacy.kv WHERE k LIKE 'notify_seen:%'").fetchall():
                fp = str(row[0]).split("notify_seen:", 1)[-1]
                try:
                    rec = json.loads(row[1])
                except Exception:
                    continue
                if not isinstance(rec, dict) or not fp:
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO notify_seen (fp, kind, head, count, first, last, issue)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (fp, "", "", max(1, int(rec.get("count") or 1)),
                     float(rec.get("first") or 0), float(rec.get("last") or 0),
                     int(rec["issue"]) if rec.get("issue") else None))
                copied += 1
            con.commit()
            helpers.kv_set("backfilled_from", json.dumps(
                {"db": str(LEGACY_DB_PATH), "prefix": "notify_seen:",
                 "rows": copied, "ts": time.time()}))
        finally:
            con.execute("DETACH DATABASE legacy")
    except Exception:
        con.rollback()                  # try again next boot; the table is still empty
        return 0
    return copied


# --- dedup + throttle ---------------------------------------------------------

def head_of(detail: str) -> str:
    """The first line, which is all the fingerprint and the issue title use.

    `.strip().splitlines()[0]` is the obvious way to write this and it is wrong:
    a detail of "\\r" is truthy, strips to "", and splits to an EMPTY list — so
    the notifier raised IndexError on the one path whose entire promise is that
    it never raises. (Found by the contract fuzzer; the pre-P2 body has the same
    hazard, which is exactly the kind of thing a committed spec is for.)
    """
    lines = (detail or "").strip().splitlines()
    return lines[0][:200] if lines else ""


def fingerprint(kind: str, detail: str) -> str:
    """What makes two faults 'the same'.

    Deliberately coarse — the first line of the message, not the whole thing.
    Stack traces differ by line numbers and timestamps between otherwise
    identical crashes, and a fingerprint that fine would file a fresh issue for
    every one.
    """
    return hashlib.sha256(f"{kind}|{head_of(detail)}".encode()).hexdigest()[:12]


def _seen(fp: str) -> dict | None:
    got = _rows("SELECT * FROM notify_seen WHERE fp=?", (fp,))
    return got[0] if got else None


def _sent_last_hour() -> int:
    return int(_rows("SELECT COUNT(*) AS n FROM notify_sent WHERE ts > ?",
                     (time.time() - 3600,))[0]["n"])


def _throttled() -> bool:
    return _sent_last_hour() >= MAX_PER_HOUR


# --- the git host, vendored minimal -------------------------------------------

def gh_enabled(repo: str) -> bool:
    return bool(GITHUB_TOKEN and repo)


def _auth_headers() -> dict:
    if GIT_PROVIDER == "gitea":
        # `token` has been accepted by every Gitea release; `Bearer` only since
        # 1.21. The swagger documents `token`, so that is what a self-hoster's
        # reverse proxy and older instance are both known to pass.
        return {"Authorization": f"token {GITHUB_TOKEN}"}
    return {"Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


async def create_issue(repo: str, title: str, body: str) -> int:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{GIT_API}/repos/{repo}/issues", headers=_auth_headers(),
                         json={"title": title, "body": body})
        r.raise_for_status()
        return int(r.json()["number"])


async def comment_issue(repo: str, number: int, body: str) -> None:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{GIT_API}/repos/{repo}/issues/{number}/comments",
                         headers=_auth_headers(), json={"body": body})
        r.raise_for_status()


# --- the bus, through the conductor's door ------------------------------------

# Tests inject an httpx transport here (an ASGI transport onto the conductor's
# app, or one that raises) — the client code path stays identical.
BUS_TRANSPORT: httpx.AsyncBaseTransport | None = None
BUS_TIMEOUT = 2.0


async def emit(kind: str, payload: dict, project_id: int = 0) -> bool:
    """Put an event on the platform bus — via the conductor, never by writing its
    events table. The bus stays single-writer (SERVICE_CONTRACT rule 5).

    Best-effort by design: an issue that was filed but not announced is a
    notification that worked. Raising here would turn a cosmetic gap into the
    failure of the thing it was reporting on.
    """
    try:
        async with httpx.AsyncClient(base_url=CONDUCTOR_URL, timeout=BUS_TIMEOUT,
                                     transport=BUS_TRANSPORT,
                                     headers={"X-Service-Token": helpers.SERVICE_TOKEN}) as c:
            r = await c.post("/internal/bus", json={
                "project_id": project_id, "task_id": None, "source": SERVICE,
                "kind": kind, "payload": payload})
            return r.status_code == 200
    except Exception:
        return False


# --- the verbs ----------------------------------------------------------------

async def report_error(kind: str, detail: str, context: dict, repo: str) -> dict:
    """File (or count) a platform fault as a GitHub issue.

    Returns what it did, so callers can log it — but never raises. A notifier that
    can break the thing it is reporting on is worse than no notifier.
    """
    if not ENABLED or not detail:
        return {"sent": False, "reason": "disabled"}
    fp = fingerprint(kind, detail)
    seen = _seen(fp)
    now = time.time()
    if seen:
        # Same fault again: count it, do not file another issue. Comment only on
        # milestones, or a busy loop becomes a busy comment thread.
        #
        # The count is incremented IN SQL rather than read-then-written: the whole
        # reason this state left the conductor's kv blob is that two processes
        # counting the same crash loop must not each write "2".
        _execute("UPDATE notify_seen SET count=count+1, last=? WHERE fp=?", (now, fp))
        count = int((_seen(fp) or {}).get("count") or seen["count"] + 1)
        issue = seen.get("issue")
        if count in (10, 100, 1000) and issue:
            try:
                await comment_issue(repo, int(issue),
                                    f"Still happening — {count} times since "
                                    f"{time.strftime('%H:%M', time.localtime(seen['first']))}.")
            except Exception:
                pass
        return {"sent": False, "reason": "already reported", "count": count,
                "issue": issue}

    head = head_of(detail)
    _execute("INSERT INTO notify_seen (fp, kind, head, count, first, last, issue)"
             " VALUES (?,?,?,1,?,?,NULL)", (fp, kind[:60], head, now, now))
    if _throttled():
        return {"sent": False, "reason": f"more than {MAX_PER_HOUR} in an hour; "
                                         f"holding off so this cannot flood"}
    if not repo or not gh_enabled(repo):
        return {"sent": False, "reason": "no GitHub repo configured to report to"}

    body = (
        f"The platform reported a fault while running unattended.\n\n"
        f"**What happened**\n```\n{detail[:2000]}\n```\n\n"
        f"**Where**\n" +
        "".join(f"- {k}: `{v}`\n" for k, v in (context or {}).items()) +
        f"- fingerprint: `{fp}`\n"
        f"- at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"_Filed automatically. Repeats of the same fault are counted on this "
        f"issue rather than filed again._"
    )
    try:
        n = await create_issue(repo, f"[{kind}] {head[:90]}", body)
        _execute("UPDATE notify_seen SET issue=? WHERE fp=?", (int(n), fp))
        _execute("INSERT INTO notify_sent (ts) VALUES (?)", (time.time(),))
        await emit("notified", {"issue": n, "kind": kind})
        return {"sent": True, "issue": n}
    except Exception as e:
        return {"sent": False, "reason": f"could not file the issue: {e}"}


async def file_issue(repo: str, title: str, body: str) -> dict:
    """The generic door: text somebody else composed, filed as an issue.

    No dedup and no fingerprint — the caller already decided this is worth
    filing (the sprint digest is the first user, and two sprints producing the
    same digest would be a coincidence, not a loop). The hourly ceiling still
    applies, because the ceiling exists to protect the token, not the inbox.
    """
    if not ENABLED:
        return {"sent": False, "reason": "disabled"}
    if _throttled():
        return {"sent": False, "reason": f"more than {MAX_PER_HOUR} in an hour; "
                                         f"holding off so this cannot flood"}
    if not repo or not gh_enabled(repo):
        return {"sent": False, "reason": "no GitHub repo configured to report to"}
    try:
        n = await create_issue(repo, title, body)
        _execute("INSERT INTO notify_sent (ts) VALUES (?)", (time.time(),))
        await emit("issue_filed", {"issue": n, "title": title[:120]})
        return {"sent": True, "issue": n}
    except Exception as e:
        return {"sent": False, "reason": f"could not file the issue: {e}"}


def status() -> dict:
    """What has been reported, for the UI — so 'no notifications' is provably
    'nothing went wrong' rather than 'the notifier is broken'."""
    return {
        "enabled": ENABLED,
        "max_per_hour": MAX_PER_HOUR,
        "sent_last_hour": _sent_last_hour(),
        "distinct_faults": [
            {"kind_hash": r["fp"], "kind": r["kind"], "head": r["head"],
             "count": r["count"], "issue": r["issue"], "last": r["last"]}
            for r in _rows("SELECT * FROM notify_seen ORDER BY last DESC LIMIT 50")],
    }


def forget(fingerprint_: str = "") -> int:
    """Stop counting a fault as already-reported, so the next occurrence files a
    fresh issue. The operational use is after a fix ships: the old issue is closed
    and a recurrence should be loud again, not silently added to a stale count."""
    if fingerprint_:
        cur = _execute("DELETE FROM notify_seen WHERE fp=?", (fingerprint_,))
        return int(cur.rowcount or 0)
    cur = _execute("DELETE FROM notify_seen", ())
    return int(cur.rowcount or 0)


init_store()
backfill_from_legacy()


# --- the HTTP surface ---------------------------------------------------------

app = FastAPI(title=SERVICE, openapi_url=None)

_MAX_TEXT = 20_000


class ErrorBody(StrictBody):
    kind: str = Field(min_length=1, max_length=120)
    detail: str = Field("", max_length=_MAX_TEXT)
    context: dict | None = None
    # The repo rides the call: which repository this platform's own faults belong
    # to is derived from the git remote, and that is conductor knowledge. Same
    # shape the knowledge service's embedding key uses — configuration travels
    # with the request rather than being duplicated into a second env.
    repo: str = Field("", max_length=200)


class IssueBody(StrictBody):
    repo: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    body: str = Field("", max_length=_MAX_TEXT)


class ForgetBody(StrictBody):
    fingerprint: str = Field("", max_length=64)     # blank = forget everything


@app.get("/health")
def health() -> dict:
    """Readiness, not liveness: ok only when the service could actually answer —
    its own database opens and the dedup table reads. Deliberately NOT gated on
    reaching GitHub: an unreachable git host is a degraded notifier, not a dead
    process, and it says so in the answer instead."""
    def _table_ok() -> bool:
        try:
            helpers.db().execute("SELECT COUNT(*) FROM notify_seen").fetchone()
            return True
        except Exception:
            return False
    checks = {"db": helpers.db_ok(), "table": _table_ok()}
    return {"ok": all(checks.values()), "service": SERVICE,
            "db": str(helpers.DB_PATH), "checks": checks}


@app.get("/openapi.json")
def openapi_spec() -> JSONResponse:
    """The committed contract. Regenerate after changing routes:
    `python app.py --spec > openapi.json` — and let oasdiff judge the diff."""
    return JSONResponse(json.loads(SPEC.read_text()))


@app.post("/error", dependencies=[Depends(helpers.require_token)],
          responses={400: {"description": "Malformed JSON body"}})
async def error_route(body: ErrorBody) -> dict:
    return await report_error(body.kind, body.detail, body.context or {}, body.repo)


@app.post("/issue", dependencies=[Depends(helpers.require_token)],
          responses={400: {"description": "Malformed JSON body"}})
async def issue_route(body: IssueBody) -> dict:
    return await file_issue(body.repo, body.title, body.body)


@app.get("/status", dependencies=[Depends(helpers.require_token)])
def status_route() -> dict:
    return status()


@app.post("/forget", dependencies=[Depends(helpers.require_token)],
          responses={400: {"description": "Malformed JSON body"}})
def forget_route(body: ForgetBody) -> dict:
    return {"removed": forget(body.fingerprint)}


if __name__ == "__main__":
    if "--spec" in sys.argv:
        print(json.dumps(app.openapi(), indent=2))
    else:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8883")))
