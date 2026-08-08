"""The usage service — one rolling meter of every model call this box makes,
whoever makes it.

The self-repair crew and the owner draw on the SAME subscription. A crew that
meters only its own sessions is measuring the wrong thing: six scout runs are
nothing on a quiet Sunday and are theft on a weekday afternoon when the owner is
mid-project. So the sleep decision needs a number the crew cannot see from inside
itself — how much of the window is actually being consumed, and by whom.

Every Claude-spending path reports here: repair scout/build sessions, the project
manager's session, worker task sessions, and studio/lifeworld seat calls. Each row
carries a SOURCE, so the meter can answer the only two questions the manager
really has:

    "is anyone else using it right now?"   → contention (yield to the human)
    "how much of this window is gone?"     → utilization (don't hit the wall)

MEASURED IN TOKENS, not dollars. On a Max subscription there is no bill, so a
dollar figure is a number nobody can act on — "$3.67" answers no question the
owner has. Tokens are what the limit is actually made of, and the SDK reports them
per session. Two counts are kept apart on purpose:

    tok    input + output — the real work, and what the meter runs on
    cache  cache reads/writes — enormous next to the rest (one build reads ~3M)
           and far cheaper; folded into one number it makes every build look
           catastrophic

`usd` is still recorded, because someone running on API keys rather than a
subscription will want it, but nothing in the sleep decision reads it.

WHY THIS IS A SERVICE, AND WHY IT IS A TABLE (the P2 extraction). The meter used
to be ONE kv blob in the conductor's database, rewritten whole on every note:
read the list, append, write the list back. That is a lost update waiting to
happen, and the moment the platform became more than one process it stopped
waiting — two notes landing together and one of them silently vanishes, on the
very number that decides whether the crew may spend the owner's quota. So the
blob is gone and `usage_rows` is a REAL table: one INSERT per call, no
read-modify-write, no lock, nothing to lose. Killing that hazard is the entire
point of the move; being a separate process is how it stops being tempting to
reach into.

ATTRIBUTION IS EXPLICIT ON THE WIRE. The conductor keeps a contextvar
(`usage.attributed("repair")`) as a convenience for its own call sites, but it is
resolved BEFORE the request — this service never sees ambient state, only a
`source` field somebody wrote down. A meter that guesses who spent is a meter
that can bill the crew's own footsteps to the owner and put the crew to sleep
forever.

KNOBS COME FROM THE CONDUCTOR, over `GET /internal/tuning` with this service's
token, cached briefly. The four dials below are the owner's, live-editable on the
Settings screen, and a meter running on a private copy of them would quietly
disagree with the screen that set them.

FIRST BOOT backfills from the conductor's legacy kv ledger (`usage:ledger`) over
a read-only ATTACH, expanding the blob into rows, exactly once.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


class StrictBody(BaseModel):
    """Strict validation: the contract says integer, so "5" is a 422, not a
    quiet coercion — lax mode is how a spec and a service drift apart."""
    model_config = ConfigDict(strict=True)


def _json_int(v):
    """JSON Schema's `integer` means "a number with zero fractional part" — 24.0
    qualifies — while Python's strict mode wants the int type itself. The
    contract is the spec, so meet ITS semantics exactly: integral numbers pass,
    strings and booleans stay rejected."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _json_float(v):
    """The mirror of the above: a JSON `number` accepts 3 as well as 3.0, and
    strict mode alone would 422 the integer."""
    if isinstance(v, int) and not isinstance(v, bool):
        return float(v)
    return v


JsonInt = Annotated[int, BeforeValidator(_json_int)]
JsonFloat = Annotated[float, BeforeValidator(_json_float)]

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))       # so `uvicorn app:app` works from any cwd
import helpers                      # vendored per service — never imported across services

SERVICE = os.environ.get("SERVICE_NAME", HERE.name)
SPEC = HERE / "openapi.json"
# The conductor's monolith store, for the one-time first-boot copy. Relative to
# the cwd process-compose runs from (the repo root) — same convention as DB_PATH.
LEGACY_DB_PATH = Path(os.environ.get("LEGACY_DB_PATH", "devteam.db"))
LEGACY_LEDGER_KEY = "usage:ledger"

CONDUCTOR_URL = (os.environ.get("CONDUCTOR_URL") or "").strip().rstrip("/")

U_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_rows (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,               -- when it was spent
    source  TEXT    NOT NULL,               -- manager | worker | studio | repair | ?
    model   TEXT    NOT NULL DEFAULT '',
    tok     INTEGER NOT NULL DEFAULT 0,     -- input+output: the real work
    cache   INTEGER NOT NULL DEFAULT 0,     -- cache reads/writes, kept apart on purpose
    usd     REAL    NOT NULL DEFAULT 0,
    calls   INTEGER NOT NULL DEFAULT 1      -- a session can stand for many calls
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_rows(ts);
CREATE INDEX IF NOT EXISTS idx_usage_source_ts ON usage_rows(source, ts);
"""

# Sources that are the OWNER's work — the crew yields to these. Anything tagged
# "repair" is the crew's own spend and must not make the crew think the box is
# busy (it would then yield to itself and never wake).
OWNER_SOURCES = ("manager", "worker", "studio")

# A meter that only grows is a landfill, and rows older than the longest window
# anyone asks about answer no question. Pruned on a counter rather than on every
# note: the whole point of the table is that a note is one cheap INSERT.
RETENTION_S = 30 * 86400
MAX_ROWS = 200_000
_PRUNE_EVERY = 500
_notes_since_prune = 0


# --- knobs: the conductor's, read through its door ----------------------------

# The values used only when this service has NEVER once reached the conductor —
# a fresh boot against a conductor that is still starting. They are deliberately
# the same numbers as the conductor's own tuning defaults, and the drill in
# tests/test_usage_service.py asserts they still are: a silently diverged copy is
# a meter that disagrees with the Settings screen that set it.
KNOB_DEFAULTS: dict[str, Any] = {
    "usage_window_h": 5.0,
    "usage_budget_tokens": 1_000_000,
    "repair_idle_share": 0.6,
    "repair_yield_quiet_s": 900,
}
KNOB_TTL = 30.0                     # seconds; tests set 0 to read through
_KNOBS: dict[str, tuple[float, Any]] = {}
# Tests inject an httpx transport here (a handler that answers as the conductor
# does) — the client code path stays identical.
TUNING_TRANSPORT: httpx.AsyncBaseTransport | None = None
TUNING_TIMEOUT = 2.0


async def knob(name: str) -> Any:
    """One tuning knob, from the conductor, cached for KNOB_TTL.

    STALE-BUT-REAL BEATS DEFAULT-BUT-WRONG: when the refresh fails, the last
    value this service actually saw is kept. Falling back to the baked default
    would silently re-widen an allowance the owner had narrowed, which is the one
    mistake a quota meter must not make.
    """
    hit = _KNOBS.get(name)
    if hit and (time.time() - hit[0]) < KNOB_TTL:
        return hit[1]
    try:
        async with httpx.AsyncClient(
                base_url=CONDUCTOR_URL, timeout=TUNING_TIMEOUT,
                transport=TUNING_TRANSPORT,
                headers={"X-Service-Token": helpers.SERVICE_TOKEN}) as c:
            r = await c.get("/internal/tuning", params={"name": name})
            r.raise_for_status()
            value = r.json()["value"]
        _KNOBS[name] = (time.time(), value)
        return value
    except Exception:
        if hit:
            return hit[1]
        return KNOB_DEFAULTS[name]


async def window_hours() -> float:
    try:
        return max(0.25, float(await knob("usage_window_h")))
    except Exception:
        return 5.0


async def budget_tokens() -> int:
    try:
        return max(1000, int(await knob("usage_budget_tokens")))
    except Exception:
        return 1_000_000


# --- this service's own store -------------------------------------------------

def _execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    con = helpers.db()
    cur = con.execute(sql, params)
    con.commit()
    return cur


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in helpers.db().execute(sql, params).fetchall()]


def init_store() -> None:
    """Own schema, own file — WAL via helpers.db(), one owner per file."""
    con = helpers.db()          # helpers.db() already sets row_factory
    con.executescript(U_SCHEMA)
    con.commit()


def backfill_from_legacy() -> int:
    """First boot only: expand the conductor's kv blob into rows, once.

    The blob is `usage:ledger` in devteam.db's kv table — a JSON list of exactly
    the dicts this table's columns are named after. Any failure (the conductor
    mid-write, a locked file) leaves the marker unset and the next boot simply
    tries again; the copy is idempotent because it only ever runs against an
    empty table.

    Rows written before work and cache were separated carry a `tokens` field that
    summed all four kinds — one build's 3M cache reads inside it. They arrive
    with tok=0, deliberately: they still count as CALLS, which is what the
    session-count backstop is for, and an honest zero beats a number we know is
    wrong.
    """
    if helpers.kv_get("backfilled_from"):
        return 0
    if _rows("SELECT COUNT(*) AS n FROM usage_rows")[0]["n"]:
        return 0
    if not LEGACY_DB_PATH.exists():
        return 0
    con = helpers.db()
    copied = 0
    try:
        con.execute("ATTACH DATABASE ? AS legacy", (f"file:{LEGACY_DB_PATH}?mode=ro",))
        try:
            got = con.execute("SELECT v FROM legacy.kv WHERE k=?",
                              (LEGACY_LEDGER_KEY,)).fetchone()
            ledger = json.loads(got[0]) if got else []
            if isinstance(ledger, list):
                for r in ledger:
                    if not isinstance(r, dict):
                        continue
                    con.execute(
                        "INSERT INTO usage_rows (ts, source, model, tok, cache, usd, calls)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (float(r.get("ts") or 0), str(r.get("source") or "?")[:24],
                         str(r.get("model") or "")[:60], max(0, int(r.get("tok") or 0)),
                         max(0, int(r.get("cache") or 0)), round(float(r.get("usd") or 0), 4),
                         max(1, int(r.get("calls") or 1))))
                    copied += 1
                con.commit()
            helpers.kv_set("backfilled_from", json.dumps(
                {"db": str(LEGACY_DB_PATH), "key": LEGACY_LEDGER_KEY,
                 "rows": copied, "ts": time.time()}))
        finally:
            con.execute("DETACH DATABASE legacy")
    except Exception:
        con.rollback()                  # try again next boot; the table is still empty
        return 0
    return copied


def _prune() -> None:
    """Drop what answers no question any more. Age first (cheap, indexed), then a
    hard ceiling in case someone sets a window measured in weeks."""
    _execute("DELETE FROM usage_rows WHERE ts < ?", (time.time() - RETENTION_S,))
    n = _rows("SELECT COUNT(*) AS n FROM usage_rows")[0]["n"]
    if n > MAX_ROWS:
        _execute("DELETE FROM usage_rows WHERE id IN ("
                 " SELECT id FROM usage_rows ORDER BY ts ASC LIMIT ?)", (n - MAX_ROWS,))


def note(source: str, model: str = "", tok: int = 0, cache: int = 0,
         usd: float = 0.0, calls: int = 1, ts: float | None = None) -> int:
    """Record one model call (or one session standing for `calls` of them).

    ONE INSERT. The kv blob this replaced did read-append-write, so two notes
    landing together lost one of them; an append-only table cannot.
    """
    global _notes_since_prune
    cur = _execute(
        "INSERT INTO usage_rows (ts, source, model, tok, cache, usd, calls)"
        " VALUES (?,?,?,?,?,?,?)",
        (float(ts or time.time()), str(source or "?")[:24], str(model or "")[:60],
         max(0, int(tok or 0)), max(0, int(cache or 0)), round(float(usd or 0), 4),
         max(1, int(calls or 1))))
    _notes_since_prune += 1
    if _notes_since_prune >= _PRUNE_EVERY:
        _notes_since_prune = 0
        try:
            _prune()
        except Exception:
            pass                # metering must never break the thing being metered
    return int(cur.lastrowid or 0)


def rows(since: float = 0.0) -> list[dict]:
    return _rows("SELECT ts, source, model, tok, cache, usd, calls FROM usage_rows"
                 " WHERE ts >= ? ORDER BY ts ASC", (float(since),))


async def snapshot(now: float | None = None) -> dict:
    """The whole utilization picture, in one dict the engine and the UI both read.

    `allowance_tok` is the heart of it: what the crew may spend in this window is
    what the OWNER left unused, capped by `repair_idle_share`. Nobody has to tune
    a crew budget — a quiet box hands the crew room automatically, and a busy one
    takes it back.

    Aggregated in SQL rather than in Python: the old body pulled the whole ledger
    into memory because it WAS one blob, and there is no reason to keep paying
    that now the rows are rows.
    """
    now = float(now or time.time())
    win = (await window_hours()) * 3600
    budget = await budget_tokens()
    share = min(1.0, max(0.0, float(await knob("repair_idle_share"))))
    quiet_need = max(0, int(await knob("repair_yield_quiet_s")))

    groups = _rows(
        "SELECT source, SUM(tok) AS tok, SUM(cache) AS cache, SUM(calls) AS calls,"
        " SUM(usd) AS usd, MAX(ts) AS last_ts, MIN(ts) AS first_ts"
        " FROM usage_rows WHERE ts >= ? GROUP BY source", (now - win,))

    by_source: dict[str, dict] = {
        str(g["source"]): {"tok": int(g["tok"] or 0), "cache": int(g["cache"] or 0),
                           "calls": int(g["calls"] or 0)}
        for g in groups}
    owner = [g for g in groups if g["source"] in OWNER_SOURCES]
    owner_tok = sum(int(g["tok"] or 0) for g in owner)
    my_tok = sum(int(g["tok"] or 0) for g in groups if g["source"] == "repair")
    total_tok = sum(int(g["tok"] or 0) for g in groups)

    last_owner = max((float(g["last_ts"] or 0) for g in owner), default=0.0)
    quiet_s = int(now - last_owner) if last_owner else 10 ** 9   # never seen = fully quiet
    oldest = min((float(g["first_ts"] or 0) for g in groups), default=0.0)
    return {
        "window_h": round(win / 3600, 2), "budget_tok": budget,
        "used_tok": total_tok, "owner_tok": owner_tok, "repair_tok": my_tok,
        "cache_tok": sum(int(g["cache"] or 0) for g in groups),
        "frac": round(min(1.0, total_tok / budget), 3),
        "owner_frac": round(min(1.0, owner_tok / budget), 3),
        "idle_frac": round(max(0.0, 1.0 - owner_tok / budget), 3),
        "allowance_tok": max(0, int(budget * share) - owner_tok),
        "quiet_s": min(quiet_s, 10 ** 9), "quiet_need_s": quiet_need,
        "contended": bool(last_owner and quiet_s < quiet_need),
        "calls": sum(int(g["calls"] or 0) for g in groups),
        "usd": round(sum(float(g["usd"] or 0) for g in groups), 2),
        "resets_at": (oldest + win) if oldest else 0.0,
        "by_source": by_source,
    }


async def verdict(now: float | None = None) -> dict:
    """(quota_is_available, why_not, wake_ts) — the utilization half of the sleep
    decision, as JSON.

    Two distinct refusals, because they want different wake times: CONTENTION is
    transient (the owner is typing right now — check back in minutes), while an
    EXHAUSTED share only clears when the rolling window rolls.
    """
    now = float(now or time.time())
    u = await snapshot(now)
    if u["contended"]:
        return {"ok": False, "why": "yielding — your own work is using the quota",
                "wake": now + max(60, u["quiet_need_s"] - u["quiet_s"])}
    if u["repair_tok"] >= u["allowance_tok"] > 0:
        return {"ok": False, "why": "self-repair has used its share of this window",
                "wake": u["resets_at"] or now + 1800}
    if u["allowance_tok"] <= 0 and u["owner_tok"] > 0:
        return {"ok": False, "why": "your own work has claimed the window's quota",
                "wake": u["resets_at"] or now + 1800}
    return {"ok": True, "why": "", "wake": 0.0}


init_store()
backfill_from_legacy()


# --- the HTTP surface ---------------------------------------------------------

# openapi_url=None: FastAPI must not serve a live-generated spec at the contract's
# address — the committed file is the contract, and drift between the two is a
# thing the contract tests exist to catch.
app = FastAPI(title=SERVICE, openapi_url=None)

# Integer bounds are contract, not decoration: SQLite INTEGER is signed 64-bit,
# and a fuzzer will happily post tok=2**63 to find out what happens. A token
# count above a trillion is not a measurement, it is a bug upstream.
_MAX_TOK = 10 ** 12
_MAX_CALLS = 10 ** 9
# Timestamps: 1970..~5138. A window computed from a nonsense `now` is arithmetic
# nobody can read, and unbounded floats include inf and nan.
_MAX_TS = 10 ** 11


class NoteBody(StrictBody):
    source: str = Field(min_length=1, max_length=24)
    model: str = Field("", max_length=60)
    tok: JsonInt = Field(0, ge=0, le=_MAX_TOK)
    cache: JsonInt = Field(0, ge=0, le=_MAX_TOK)
    usd: JsonFloat = Field(0.0, ge=0, le=10 ** 6)
    calls: JsonInt = Field(1, ge=1, le=_MAX_CALLS)
    ts: JsonFloat = Field(0.0, ge=0, le=_MAX_TS)   # 0 = now


@app.get("/health")
def health() -> dict:
    """Readiness, not liveness: ok only when the service could actually answer —
    its own database opens and the meter's table reads.

    Deliberately NOT gated on reaching the conductor for knobs: the meter can
    answer with the knob values it already has, and a readiness probe that goes
    red because a PEER is restarting is how a fleet takes itself down in a ring.
    """
    def _table_ok() -> bool:
        try:
            helpers.db().execute("SELECT COUNT(*) FROM usage_rows").fetchone()
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


@app.post("/note", dependencies=[Depends(helpers.require_token)],
          responses={400: {"description": "Malformed JSON body"}})
def note_route(body: NoteBody) -> dict:
    """Record one call. `source` is REQUIRED and always explicit: this service
    has no contextvar, no default, and no opinion about who is spending."""
    return {"id": note(body.source, body.model, tok=body.tok, cache=body.cache,
                       usd=body.usd, calls=body.calls, ts=body.ts or None)}


@app.get("/snapshot", dependencies=[Depends(helpers.require_token)])
async def snapshot_route(now: float = Query(0, ge=0, le=_MAX_TS)) -> dict:
    return await snapshot(now or None)


@app.get("/verdict", dependencies=[Depends(helpers.require_token)])
async def verdict_route(now: float = Query(0, ge=0, le=_MAX_TS)) -> dict:
    return await verdict(now or None)


@app.get("/rows", dependencies=[Depends(helpers.require_token)])
def rows_route(since: float = Query(0, ge=0, le=_MAX_TS)) -> dict:
    return {"rows": rows(since)}


if __name__ == "__main__":
    if "--spec" in sys.argv:
        # The one honest way to update the contract: print what the code actually
        # serves, commit it, and let the tests + oasdiff hold you to it.
        print(json.dumps(app.openapi(), indent=2))
    else:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8882")))
