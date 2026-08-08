"""The knowledge service — what an agent has learned, stored so it can be found again.

The P1 extraction: the whole body of the conductor's old knowledge.py — the two
embedders, the blended scoring, the four verbs, the per-owner prune — now one
process behind one port with one committed contract, owning data/knowledge.db
alone. The conductor talks to it through its shim (conductor/app/knowledge.py)
and documents the degraded modes; nothing else may open this database.

A generic black box behind four verbs:

    POST /remember   what happened, and what to take from it
    POST /recall     the nearest things an owner already knows
    POST /reinforce  how that turned out, next time it was used
    POST /forget     because a knowledge base that only grows is a landfill

plus GET /stats (what the store holds), POST /tokens (the tokenizer as contract —
the lifeworld's leak-checks and recall must agree on what a word is), and the
contract-mandated GET /health and GET /openapi.json.

The distinction that makes retrieval work is CUE versus SAYS. The cue is the
SITUATION — "the build failed with ImportError: no module named app" — and it is
the only thing a query is ever matched against. The says is the LESSON — "an
ImportError here means the venv symlink, not the code" — and it is what comes
back. Embedding the lesson instead of the situation is the classic mistake: you
then retrieve by similarity to answers, and an agent that already knew the answer
would not be asking.

TWO BACKENDS, chosen per request: where the caller's settings carry a provider
key, real embeddings; where none does, a deterministic hashed n-gram vector that
costs nothing, needs no dependency, works offline and is stable across restarts.
Model credentials ride each request body (`settings`) and are never stored —
env-only config holds no secrets beyond this service's own token.

FIRST BOOT backfills from the conductor's legacy store: if this service's table
is empty and the old devteam.db still has one (named `knowledge`, or
`knowledge_legacy` after the shim's URL-mode rename), the rows are copied over a
read-only ATTACH and a marker is set so the copy happens exactly once.
"""

import array
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse
from typing import Annotated

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


JsonInt = Annotated[int, BeforeValidator(_json_int)]

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))       # so `uvicorn app:app` works from any cwd
import helpers                      # vendored per service — never imported across services

SERVICE = os.environ.get("SERVICE_NAME", HERE.name)
SPEC = HERE / "openapi.json"
# The conductor's monolith store, for the one-time first-boot copy. Relative to
# the cwd process-compose runs from (the repo root) — same convention as DB_PATH.
LEGACY_DB_PATH = Path(os.environ.get("LEGACY_DB_PATH", "devteam.db"))

K_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    owner    TEXT NOT NULL,                    -- 'lw:2:30' (an agent) or 'global'
    kind     TEXT NOT NULL DEFAULT 'belief',   -- belief | episode | note
    sig      TEXT NOT NULL DEFAULT '',         -- the exact key, when there is one
    cue      TEXT NOT NULL,                    -- the SITUATION; the only thing queries match
    says     TEXT NOT NULL,                    -- the LESSON; what comes back
    payload  TEXT NOT NULL DEFAULT '{}',
    backend  TEXT NOT NULL,                    -- which embedder made vec
    dim      INTEGER NOT NULL,
    vec      BLOB NOT NULL,
    good     INTEGER NOT NULL DEFAULT 0,
    bad      INTEGER NOT NULL DEFAULT 0,
    used     INTEGER NOT NULL DEFAULT 0,
    ts       REAL NOT NULL,
    UNIQUE(owner, kind, sig, cue)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_owner ON knowledge(owner, kind);
"""

DIM = 256                      # the local backend's width
LOCAL = f"hash-{DIM}"
MAX_PER_OWNER = 2000           # a knowledge base that only grows is a landfill
# Applied to RELEVANCE, not to the final score — otherwise a lesson with a perfect record
# clears the bar on its record alone, which is how a knowledge base starts confidently
# answering questions nobody asked.
FLOOR = 0.10

_WORD = re.compile(r"[a-z0-9_]+")

# IDF alone cannot separate "on" from "8787" until the corpus is large, and a knowledge base
# is smallest exactly when it is newest — so the words that carry no information about a
# situation are named outright rather than waited for.
_STOP = frozenset("""
a an and are as at be been being but by can could did do does for from had has have he her
his how i if in into is it its me my no nor not of on once only or our out over own same she
so some such than that the their them then there these they this those to too under until up
very was we were what when where which while who why will with would you your it's we're
""".split())


def _useful(t: str) -> bool:
    """A token worth matching on. Single characters and bare small integers are the debris of
    tokenising paths and versions ("127.0.0.1" → 127, 0, 0, 1) and match everything."""
    if t in _STOP:
        return False
    if len(t) < 2:
        return False
    return not (t.isdigit() and len(t) < 3)


# --- the local embedder: free, offline, deterministic ------------------------

def _tokens(text: str) -> list[str]:
    # Served at POST /tokens: the lifeworld substrate consumes this through the
    # conductor shim (lifeworld/ports.knowledge_tokens) so leak-checks and recall
    # agree on what a word is — it is contract now, not a private reach-in.
    return [t for t in _WORD.findall((text or "").lower())[:300] if _useful(t)][:200]


def _features(text: str) -> Iterable[tuple[str, float]]:
    """Words, word bigrams, and character 4-grams.

    Character grams are what make it survive the things that actually vary between two
    reports of one situation: a path, a hostname, a typo, a British/American spelling.
    """
    toks = _tokens(text)
    for t in toks:
        yield ("w:" + t, 1.0)
    for a, b in zip(toks, toks[1:]):
        yield (f"b:{a}_{b}", 1.3)          # a pair is more specific than either word
    flat = " ".join(toks)
    for i in range(0, max(0, len(flat) - 3)):
        yield ("c:" + flat[i:i + 4], 0.35)


def embed_local(text: str) -> array.array:
    """A hashed bag-of-features vector, L2-normalised. No model, no network, no drift."""
    v = array.array("f", [0.0]) * DIM
    for feat, weight in _features(text):
        h = hashlib.blake2b(feat.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "big") % DIM
        sign = 1.0 if h[4] & 1 else -1.0        # signed hashing cancels collisions on average
        v[idx] += sign * weight
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    for i in range(DIM):
        v[i] /= norm
    return v


async def embed_remote(texts: list[str], settings: dict) -> tuple[str, int, list[array.array]] | None:
    """Real embeddings, when a provider key exists. None if none does — never a hard failure:
    knowledge that stops being stored because a key expired is worse than coarse knowledge."""
    key = (settings or {}).get("openai_api_key")
    if not key:
        return None
    try:
        import httpx
        model = "text-embedding-3-small"
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://api.openai.com/v1/embeddings",
                             headers={"Authorization": f"Bearer {key}"},
                             json={"model": model, "input": texts[:64]})
            r.raise_for_status()
            rows = r.json().get("data") or []
        out = [array.array("f", d["embedding"]) for d in rows]
        if not out:
            return None
        return f"openai:{model}", len(out[0]), out
    except Exception:
        return None


async def embed(texts: list[str], settings: dict | None = None) -> tuple[str, int, list[array.array]]:
    got = await embed_remote(texts, settings or {})
    if got:
        return got
    return LOCAL, DIM, [embed_local(t) for t in texts]


def _blob(v: array.array) -> bytes:
    return v.tobytes()


def _vec(blob: bytes, dim: int) -> array.array:
    a = array.array("f")
    a.frombytes(blob[:dim * 4])
    return a


def _cos(a: array.array, b: array.array) -> float:
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))      # both are unit vectors


# --- this service's own store ------------------------------------------------

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
    con.executescript(K_SCHEMA)
    con.commit()


def backfill_from_legacy() -> int:
    """First boot only: copy the conductor's old rows into this service's store.

    The strangler seam, honest about ordering: the conductor's URL-mode shim
    renames its table knowledge → knowledge_legacy on its own first boot, and
    the two processes start in no guaranteed order — so BOTH names are checked.
    Any failure (the conductor mid-write, a locked file) leaves the marker unset,
    and the next boot simply tries again; the copy is idempotent because it only
    ever runs against an empty table.
    """
    if helpers.kv_get("backfilled_from"):
        return 0
    n_own = _rows("SELECT COUNT(*) AS n FROM knowledge")[0]["n"]
    if n_own:
        return 0
    if not LEGACY_DB_PATH.exists():
        return 0
    con = helpers.db()
    copied = 0
    try:
        con.execute("ATTACH DATABASE ? AS legacy",
                    (f"file:{LEGACY_DB_PATH}?mode=ro",))
        try:
            tables = {r["name"] for r in con.execute(
                "SELECT name FROM legacy.sqlite_master WHERE type='table'"
                " AND name IN ('knowledge','knowledge_legacy')").fetchall()}
            src = "knowledge_legacy" if "knowledge_legacy" in tables else \
                  "knowledge" if "knowledge" in tables else ""
            if src:
                cols = ("id, owner, kind, sig, cue, says, payload, backend, dim,"
                        " vec, good, bad, used, ts")
                con.execute(f"INSERT INTO main.knowledge ({cols})"
                            f" SELECT {cols} FROM legacy.{src}")
                copied = con.execute("SELECT changes()").fetchone()[0]
                con.commit()
                helpers.kv_set("backfilled_from", json.dumps(
                    {"db": str(LEGACY_DB_PATH), "table": src, "rows": copied,
                     "ts": time.time()}))
        finally:
            con.execute("DETACH DATABASE legacy")
    except Exception:
        con.rollback()                  # try again next boot; the table is still empty
    return copied


# --- the four verbs (bodies moved from conductor/app/knowledge.py) -----------

async def remember(owner: str, cue: str, says: str, *, kind: str = "belief", sig: str = "",
                   payload: dict | None = None, good: int = 0, bad: int = 0,
                   settings: dict | None = None) -> int:
    """Store one thing worth finding again. Upserts on (owner, kind, sig, cue)."""
    cue, says = str(cue or "").strip()[:1000], str(says or "").strip()[:1000]
    if not cue or not says:
        return 0
    backend, dim, vecs = await embed([cue], settings)
    _execute(
        "INSERT INTO knowledge (owner, kind, sig, cue, says, payload, backend, dim, vec,"
        " good, bad, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(owner, kind, sig, cue) DO UPDATE SET"
        " says=excluded.says, payload=excluded.payload, backend=excluded.backend,"
        " dim=excluded.dim, vec=excluded.vec, good=knowledge.good+excluded.good,"
        " bad=knowledge.bad+excluded.bad, ts=excluded.ts",
        (owner, kind, sig, cue, says, json.dumps(payload or {}), backend, dim,
         _blob(vecs[0]), int(good), int(bad), time.time()))
    _prune(owner)
    row = _rows("SELECT id FROM knowledge WHERE owner=? AND kind=? AND sig=? AND cue=?",
                (owner, kind, sig, cue))
    return int(row[0]["id"]) if row else 0


async def recall(owner: str, query: str, k: int = 5, *, kind: str = "",
                 settings: dict | None = None, include_global: bool = True) -> list[dict]:
    """The nearest things this agent already knows about a situation like this one.

    Every hit says WHY it matched. A retrieval you cannot explain is one nobody trusts twice,
    and on a blended score the reason is genuinely not obvious — a hit can win on wording, on
    a shared rare term, or on having been right ten times before.
    """
    query = str(query or "").strip()
    if not query:
        return []
    owners = [owner] + (["global"] if include_global and owner != "global" else [])
    where = "owner IN (%s)" % ",".join("?" for _ in owners)
    args: list[Any] = list(owners)
    if kind:
        where += " AND kind=?"
        args.append(kind)
    rows = _rows(f"SELECT * FROM knowledge WHERE {where} ORDER BY ts DESC LIMIT 800", tuple(args))
    if not rows:
        return []

    backend, dim, vecs = await embed([query], settings)
    qv = vecs[0]
    qt = set(_tokens(query))
    # IDF over what this owner actually knows, computed on the rows already in hand. Without
    # it, "the" counts as much as "ImportError" and every cue looks equally like every other:
    # the words that identify a situation are exactly the ones almost nothing else contains.
    df: dict[str, int] = {}
    cues = []
    for r in rows:
        ct = set(_tokens(r["cue"]))
        cues.append(ct)
        for t in ct:
            df[t] = df.get(t, 0) + 1
    n_docs = max(1, len(rows))

    def idf(t: str) -> float:
        return math.log(1.0 + n_docs / (1.0 + df.get(t, 0)))

    now = time.time()
    out = []
    for r, ct in zip(rows, cues):
        # Never score across backends: a hashed vector against a neural one is not a worse
        # answer, it is a meaningless one. Re-embed the row locally instead of skipping it —
        # knowledge that vanishes because a key was added is worse than coarse knowledge.
        if r["backend"] == backend and int(r["dim"]) == dim:
            cos = _cos(qv, _vec(r["vec"], int(r["dim"])))
        elif backend == LOCAL:
            cos = _cos(qv, embed_local(r["cue"]))
        else:
            cos = _cos(embed_local(query), embed_local(r["cue"]))
        # How much of what the QUERY is about this cue covers — not Jaccard, which punishes
        # a long cue for being detailed and would rather match a short vague one. Retrieval
        # asks "does this answer my question", not "are these two texts the same size".
        shared = sum(idf(t) for t in (qt & ct))
        asked = sum(idf(t) for t in qt) or 1.0
        lex = min(1.0, shared / asked)
        ev = int(r["good"]) + int(r["bad"])
        conf = (int(r["good"]) / ev) if ev else 0.0
        age_days = max(0.0, (now - float(r["ts"])) / 86400)
        fresh = 1.0 / (1.0 + age_days / 30.0)
        # RELEVANCE decides; the priors only modulate. Adding a track record and a recency
        # bonus to the score let a lesson that has always worked outrank one that is actually
        # about the question — every row scored ~0.2 and the ordering was noise. A prior is
        # a tie-breaker, so it belongs as a multiplier near 1, never as a term of its own.
        # Weighted by which signal is actually trustworthy here. A neural embedding knows
        # that ModuleNotFoundError and ImportError are the same family; a hashed one only
        # knows they share letters, and pretending otherwise makes the score a lie.
        w_cos = 0.70 if backend != LOCAL else 0.30
        rel = w_cos * max(0.0, cos) + (1.0 - w_cos) * lex
        if rel < FLOOR:
            continue
        score = rel * (0.90 + 0.10 * conf) * (0.95 + 0.05 * fresh)
        out.append({
            "id": r["id"], "owner": r["owner"], "kind": r["kind"], "sig": r["sig"],
            "cue": r["cue"], "says": r["says"], "payload": json.loads(r["payload"] or "{}"),
            "good": r["good"], "bad": r["bad"], "evidence": ev, "confidence": round(conf, 3),
            "score": round(score, 4),
            "why": {"similarity": round(cos, 3), "shared_terms": round(lex, 3),
                    "relevance": round(rel, 3), "held_up": round(conf, 3),
                    "recency": round(fresh, 3),
                    # The rare words that actually earned it — the readable half of "why".
                    "matched": sorted((qt & ct), key=lambda t: -idf(t))[:5]},
        })
    out.sort(key=lambda h: -h["score"])
    return out[:max(1, min(int(k), 25))]


def reinforce(row_id: int, outcome: str) -> bool:
    """How it turned out the time this was used. The only thing that moves confidence."""
    if outcome not in ("good", "bad"):
        return False
    _execute(f"UPDATE knowledge SET {outcome}={outcome}+1, used=used+1 WHERE id=?", (int(row_id),))
    return True


def forget(owner: str, *, row_id: int = 0, sig: str = "") -> int:
    if row_id:
        _execute("DELETE FROM knowledge WHERE id=? AND owner=?", (int(row_id), owner))
        return 1
    if sig:
        _execute("DELETE FROM knowledge WHERE owner=? AND sig=?", (owner, sig))
        return 1
    _execute("DELETE FROM knowledge WHERE owner=?", (owner,))
    return 1


def _prune(owner: str) -> None:
    """Keep the useful and the recent; drop what has never helped and is old.

    Ranked by how often it has been right, then by recency — so a lesson that keeps working
    survives forever and a one-off observation nobody has used decays out.
    """
    n = _rows("SELECT COUNT(*) AS n FROM knowledge WHERE owner=?", (owner,))[0]["n"]
    if n <= MAX_PER_OWNER:
        return
    _execute(
        "DELETE FROM knowledge WHERE id IN ("
        " SELECT id FROM knowledge WHERE owner=?"
        " ORDER BY (good - bad) ASC, used ASC, ts ASC LIMIT ?)",
        (owner, int(n - MAX_PER_OWNER)))


def stats(owner: str = "") -> dict:
    where, args = ("WHERE owner=?", (owner,)) if owner else ("", ())
    rows = _rows(f"SELECT owner, kind, COUNT(*) AS n, SUM(good) AS g, SUM(bad) AS b"
                 f" FROM knowledge {where} GROUP BY owner, kind", args)
    return {"rows": rows,
            "total": sum(int(r["n"]) for r in rows),
            "backends": _rows(
                "SELECT backend, COUNT(*) AS n FROM knowledge GROUP BY backend", ())}


init_store()
backfill_from_legacy()


# --- the HTTP surface --------------------------------------------------------

# openapi_url=None: FastAPI must not serve a live-generated spec at the contract's
# address — the committed file is the contract, and drift between the two is a
# thing the contract tests exist to catch.
app = FastAPI(title=SERVICE, openapi_url=None)


# Integer bounds are contract, not decoration: SQLite INTEGER is signed 64-bit,
# and the fuzzer proved bad=2**63 turns into a 500 without them. Ids are bounded
# at 2**53-1 — the largest integer a JSON double carries exactly — because a
# bound any closer to 2**63 straddles float rounding: float(2**63-1) rounds UP,
# so a generator's schema-compliant number could exceed the very bound it read.
_MAX_ROWID = 2**53 - 1
_MAX_COUNT = 1_000_000


class RecallBody(StrictBody):
    owner: str
    query: str
    k: JsonInt = Field(5, ge=1, le=25)  # recall never returns more than 25 anyway
    kind: str = ""
    include_global: bool = True
    settings: dict | None = None        # the embedding key rides the request, never the env


class RememberBody(StrictBody):
    owner: str
    cue: str
    says: str
    kind: str = "belief"
    sig: str = ""
    payload: dict | None = None
    good: JsonInt = Field(0, ge=0, le=_MAX_COUNT)
    bad: JsonInt = Field(0, ge=0, le=_MAX_COUNT)
    settings: dict | None = None


class ReinforceBody(StrictBody):
    id: JsonInt = Field(ge=0, le=_MAX_ROWID)
    outcome: str                        # good | bad — anything else is a recorded no-op


class ForgetBody(StrictBody):
    owner: str
    row_id: JsonInt = Field(0, ge=0, le=_MAX_ROWID)
    sig: str = ""


class TokensBody(StrictBody):
    text: str


@app.get("/health")
def health() -> dict:
    """Readiness, not liveness: ok only when the service could actually answer —
    its own database opens and the knowledge table reads."""
    def _table_ok() -> bool:
        try:
            helpers.db().execute("SELECT COUNT(*) FROM knowledge").fetchone()
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


@app.post("/recall", dependencies=[Depends(helpers.require_token)],
          responses={400: {"description": "Malformed JSON body"}})
async def recall_route(body: RecallBody) -> dict:
    hits = await recall(body.owner, body.query, k=body.k, kind=body.kind,
                        settings=body.settings, include_global=body.include_global)
    return {"hits": hits}


@app.post("/remember", dependencies=[Depends(helpers.require_token)],
          responses={400: {"description": "Malformed JSON body"}})
async def remember_route(body: RememberBody) -> dict:
    row_id = await remember(body.owner, body.cue, body.says, kind=body.kind,
                            sig=body.sig, payload=body.payload, good=body.good,
                            bad=body.bad, settings=body.settings)
    return {"id": row_id}


@app.post("/reinforce", dependencies=[Depends(helpers.require_token)],
          responses={400: {"description": "Malformed JSON body"}})
def reinforce_route(body: ReinforceBody) -> dict:
    return {"ok": reinforce(body.id, body.outcome)}


@app.post("/forget", dependencies=[Depends(helpers.require_token)],
          responses={400: {"description": "Malformed JSON body"}})
def forget_route(body: ForgetBody) -> dict:
    return {"removed": forget(body.owner, row_id=body.row_id, sig=body.sig)}


@app.get("/stats", dependencies=[Depends(helpers.require_token)])
def stats_route(owner: str = Query("")) -> dict:
    return stats(owner)


@app.post("/tokens", dependencies=[Depends(helpers.require_token)],
          responses={400: {"description": "Malformed JSON body"}})
def tokens_route(body: TokensBody) -> dict:
    return {"tokens": _tokens(body.text)}


if __name__ == "__main__":
    if "--spec" in sys.argv:
        # The one honest way to update the contract: print what the code actually
        # serves, commit it, and let the tests + oasdiff hold you to it.
        print(json.dumps(app.openapi(), indent=2))
    else:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8881")))
