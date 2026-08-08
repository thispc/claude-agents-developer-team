"""Persistence — a whole World is one JSON blob, saved and loaded whole.

A Lifeworld is a playground read and rewritten per scan, not queried column by
column, so serialising the entire World (its cast, scenes, ledgers) to one row is
the honest and simplest fit. Since P4 that row lives in THIS service's own
`data/lifeworld.db` and nothing else opens the file — the conductor asks over
HTTP like anyone else.

It also decides the *runtime*: whether a loaded world can spend. By default it
loads FREE — the deterministic Tier-0 appraiser — so operating the Studio costs
nothing. `live=True` wires the MODEL DOOR in: `ports.providers(source).complete`,
which posts to the conductor's `POST /internal/complete`. What used to be the
owner's settings dict is now a `settings_ref` — an opaque string the conductor
minted and only the conductor can resolve — so a world can spend the owner's
quota without this process ever holding a credential.

WHY THIS FILE IS NOT UNDER substrate/. It is the one part of the old package that
was never substrate: it opens a database. Keeping it beside app.py means
`substrate/` is pure logic that binds to nothing, which is what lets the
conductor's suite and this service's own suite each mount the service with their
own store in one interpreter.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import helpers                      # vendored per service — never imported across services
from substrate import ports
from substrate.config import Flags
from substrate.world import World

SCHEMA = """
CREATE TABLE IF NOT EXISTS lw_worlds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lw_worlds_owner ON lw_worlds(owner_id);
"""


def init_store() -> None:
    con = helpers.db()
    con.executescript(SCHEMA)
    con.commit()


# SQLite's INTEGER is signed 64-bit, and a bigger one is not a row that is missing
# — it is an OverflowError three frames down, which reaches a caller as a 500. An
# id no row can ever have is an id that does not exist, so it answers like one: the
# contract fuzzer found this by asking for world 2**63, and "no such world of
# yours" is both true and already the documented answer.
_MAX_ID = 2**63 - 1


def _real_id(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if -_MAX_ID - 1 <= n <= _MAX_ID else None


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in helpers.db().execute(sql, params).fetchall()]


def _execute(sql: str, params: tuple = ()):
    con = helpers.db()
    cur = con.execute(sql, params)
    con.commit()
    return cur


# --- the five row verbs (bodies moved from the conductor's db.py) ------------

def create_row(owner_id: int, name: str, data: str) -> int:
    now = time.time()
    cur = _execute("INSERT INTO lw_worlds (owner_id, name, data, created_at, updated_at)"
                   " VALUES (?,?,?,?,?)", (owner_id, name, data, now, now))
    return int(cur.lastrowid)


def get_row(world_id: int) -> dict | None:
    wid = _real_id(world_id)
    if wid is None:
        return None
    rows = _rows("SELECT * FROM lw_worlds WHERE id=?", (wid,))
    return rows[0] if rows else None


def list_rows(owner_id: int) -> list[dict]:
    oid = _real_id(owner_id)
    if oid is None:
        return []
    return _rows("SELECT id, owner_id, name, updated_at FROM lw_worlds WHERE owner_id=?"
                 " ORDER BY id DESC", (oid,))


def update_row(world_id: int, data: str, name: str | None = None) -> None:
    wid = _real_id(world_id)
    if wid is None:
        return
    if name is None:
        _execute("UPDATE lw_worlds SET data=?, updated_at=? WHERE id=?",
                 (data, time.time(), wid))
    else:
        _execute("UPDATE lw_worlds SET data=?, name=?, updated_at=? WHERE id=?",
                 (data, name, time.time(), wid))


def delete_row(world_id: int) -> None:
    wid = _real_id(world_id)
    if wid is not None:
        _execute("DELETE FROM lw_worlds WHERE id=?", (wid,))


# --- the World facade --------------------------------------------------------

def create(owner_id: int, name: str, preset: str = "sandbox") -> World:
    w = World(name=name, flags=Flags.preset(preset))
    w.id = create_row(owner_id, name, "{}")
    save(w)
    return w


def owns(owner_id: int, world_id: int) -> bool:
    row = get_row(world_id)
    oid = _real_id(owner_id)
    return bool(row and oid is not None and int(row["owner_id"]) == oid)


def owner_of(world_id: int) -> int:
    row = get_row(world_id)
    return int(row["owner_id"]) if row else 0


def load(world_id: int, *, live: bool = False, settings_ref: str = "",
         source: str = "studio") -> World | None:
    row = get_row(world_id)
    if not row:
        return None
    runtime: dict[str, Any] = {}
    if live:
        runtime = {"complete": ports.providers(source).complete,
                   # the OPAQUE reference, sitting exactly where the settings dict
                   # used to sit — the substrate passes it along and cannot read it
                   "settings": settings_ref or "",
                   "model_name": _knob("scene_default_model", "claude-haiku-4-5"),
                   "utter_tokens": int(_knob("scene_utterance_max_tokens", 200))}
    w = World.from_dict(json.loads(row["data"] or "{}"), **runtime)
    w.id = int(world_id)
    return w


def _knob(name: str, default):
    """A knob through the tuning door, with the conductor's own default as the
    floor. A live world that could not read the dial still runs — on the number
    the conductor would have given it — rather than refusing to think."""
    try:
        v = ports.tuning().get(name)
        return default if v is None else v
    except Exception:
        return default


def save(world: World) -> None:
    update_row(world.id, json.dumps(world.to_dict()), name=world.name)


def listing(owner_id: int) -> list[dict]:
    return list_rows(owner_id)


def delete(world_id: int) -> None:
    delete_row(world_id)


# One lock per world, so a load→await→save cycle cannot be interleaved with another.
#
# A World is deserialized fresh on every request and saved back as a whole blob, so two
# overlapping cycles are a lost update: the self-repair crew's sprint runs `load → await
# run_deliberation → save` while the crew chat does the same round trip, and whichever
# finished last silently erased the other's memo, log and results.
#
# P4 made this lock LOAD-BEARING in a new way. The conductor used to hold it around its
# own load…save cycles; it cannot any more, because it has no World to hold one over. So
# every read-modify-write now happens INSIDE this process, under this lock, and a caller
# that wants one has to ask for a whole behaviour (deliberate, consult, seat the crew)
# rather than for the pieces. There is no way to straddle the boundary, which is the
# point: a lock you can only take on one side of a wire is not a lock.
_WORLD_LOCKS: dict[int, asyncio.Lock] = {}


def lock_for(world_id: int) -> asyncio.Lock:
    """`async with store.lock_for(wid):` around any load…mutate…save."""
    lk = _WORLD_LOCKS.get(int(world_id))
    if lk is None:
        lk = _WORLD_LOCKS[int(world_id)] = asyncio.Lock()
    return lk


# --- the one-time copy out of the conductor's monolith store -----------------

def backfill_from_legacy(legacy_db_path) -> int:
    """First boot only: copy the conductor's lw_worlds into this service's store.

    The strangler seam, honest about ordering: the conductor's URL-mode shim renames
    its table lw_worlds → lw_worlds_legacy on its own first boot, and the two
    processes start in no guaranteed order — so BOTH names are checked. Any failure
    (the conductor mid-write, a locked file) leaves the marker unset and the next boot
    tries again; the copy is idempotent because it only ever runs against an empty
    table, and the ROWIDS ARE PRESERVED because every world id in the platform —
    repair:world, graph:pool:0, a project's team pointer — is a pointer at one.
    """
    from pathlib import Path
    legacy = Path(legacy_db_path)
    if helpers.kv_get("backfilled_from"):
        return 0
    if _rows("SELECT COUNT(*) AS n FROM lw_worlds")[0]["n"]:
        return 0
    if not legacy.exists():
        return 0
    con = helpers.db()
    copied = 0
    try:
        con.execute("ATTACH DATABASE ? AS legacy", (f"file:{legacy}?mode=ro",))
        try:
            tables = {r["name"] for r in con.execute(
                "SELECT name FROM legacy.sqlite_master WHERE type='table'"
                " AND name IN ('lw_worlds','lw_worlds_legacy')").fetchall()}
            src = "lw_worlds_legacy" if "lw_worlds_legacy" in tables else \
                  "lw_worlds" if "lw_worlds" in tables else ""
            if src:
                cols = "id, owner_id, name, data, created_at, updated_at"
                con.execute(f"INSERT INTO main.lw_worlds ({cols})"
                            f" SELECT {cols} FROM legacy.{src}")
                copied = con.execute("SELECT changes()").fetchone()[0]
                con.commit()
                helpers.kv_set("backfilled_from", json.dumps(
                    {"db": str(legacy), "table": src, "rows": copied, "ts": time.time()}))
        finally:
            con.execute("DETACH DATABASE legacy")
    except Exception:
        con.rollback()                  # try again next boot; the table is still empty
    return copied
