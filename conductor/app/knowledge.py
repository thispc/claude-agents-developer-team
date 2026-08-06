"""knowledge.py — what an agent has learned, stored so it can be found again.

A generic black box behind four verbs:

    remember(owner, cue, says)   what happened, and what to take from it
    recall(owner, query, k)      the nearest things it already knows
    reinforce(id, good|bad)      how that turned out, next time it was used
    forget(owner, ...)           because a knowledge base that only grows is a landfill

The distinction that makes retrieval work is CUE versus SAYS. The cue is the SITUATION —
"the build failed with ImportError: no module named app" — and it is the only thing a query
is ever matched against. The says is the LESSON — "an ImportError here means the venv
symlink, not the code" — and it is what comes back. Embedding the lesson instead of the
situation is the classic mistake: you then retrieve by similarity to answers, and an agent
that already knew the answer would not be asking.

WHY EMBEDDINGS AND NOT ONLY THE EXACT KEY. `decisions.signature()` is a coarse exact key
(`error:ImportError`), which catches the same lesson worded differently but cannot catch a
situation that is merely LIKE one seen before — a different exception with the same cause, a
timeout that is really the same misconfiguration. That is the whole gap this closes.

TWO BACKENDS, and the choice is the platform's usual one. Where a provider key exists, real
embeddings; where none does, a deterministic hashed n-gram vector that costs nothing, needs
no dependency, works offline and is stable across restarts. Every row records WHICH backend
produced its vector, because comparing a hashed vector to a neural one is not a worse
answer, it is a meaningless one — so rows from another backend are re-embedded on demand
rather than silently scored against.

RETRIEVAL IS BLENDED, not pure cosine. A hashed embedder is decent at wording and poor at
meaning, an exact term match is the reverse, and both are blind to whether a lesson has ever
actually worked. So the score mixes similarity, literal term overlap, how well the lesson has
held up, and how recent it is — and every hit reports which of those earned it, because a
retrieval you cannot explain is one nobody will trust twice.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import re
import time
from typing import Any, Iterable

from . import db

SCHEMA = """
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


# --- the four verbs ---------------------------------------------------------

async def remember(owner: str, cue: str, says: str, *, kind: str = "belief", sig: str = "",
                   payload: dict | None = None, good: int = 0, bad: int = 0,
                   settings: dict | None = None) -> int:
    """Store one thing worth finding again. Upserts on (owner, kind, sig, cue)."""
    cue, says = str(cue or "").strip()[:1000], str(says or "").strip()[:1000]
    if not cue or not says:
        return 0
    backend, dim, vecs = await embed([cue], settings)
    db._execute(
        "INSERT INTO knowledge (owner, kind, sig, cue, says, payload, backend, dim, vec,"
        " good, bad, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(owner, kind, sig, cue) DO UPDATE SET"
        " says=excluded.says, payload=excluded.payload, backend=excluded.backend,"
        " dim=excluded.dim, vec=excluded.vec, good=knowledge.good+excluded.good,"
        " bad=knowledge.bad+excluded.bad, ts=excluded.ts",
        (owner, kind, sig, cue, says, json.dumps(payload or {}), backend, dim,
         _blob(vecs[0]), int(good), int(bad), time.time()))
    _prune(owner)
    row = db._rows("SELECT id FROM knowledge WHERE owner=? AND kind=? AND sig=? AND cue=?",
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
    rows = db._rows(f"SELECT * FROM knowledge WHERE {where} ORDER BY ts DESC LIMIT 800", tuple(args))
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


def reinforce(row_id: int, outcome: str) -> None:
    """How it turned out the time this was used. The only thing that moves confidence."""
    if outcome not in ("good", "bad"):
        return
    db._execute(f"UPDATE knowledge SET {outcome}={outcome}+1, used=used+1 WHERE id=?", (int(row_id),))


def forget(owner: str, *, row_id: int = 0, sig: str = "") -> int:
    if row_id:
        db._execute("DELETE FROM knowledge WHERE id=? AND owner=?", (int(row_id), owner))
        return 1
    if sig:
        db._execute("DELETE FROM knowledge WHERE owner=? AND sig=?", (owner, sig))
        return 1
    db._execute("DELETE FROM knowledge WHERE owner=?", (owner,))
    return 1


def _prune(owner: str) -> None:
    """Keep the useful and the recent; drop what has never helped and is old.

    Ranked by how often it has been right, then by recency — so a lesson that keeps working
    survives forever and a one-off observation nobody has used decays out.
    """
    n = db._rows("SELECT COUNT(*) AS n FROM knowledge WHERE owner=?", (owner,))[0]["n"]
    if n <= MAX_PER_OWNER:
        return
    db._execute(
        "DELETE FROM knowledge WHERE id IN ("
        " SELECT id FROM knowledge WHERE owner=?"
        " ORDER BY (good - bad) ASC, used ASC, ts ASC LIMIT ?)",
        (owner, int(n - MAX_PER_OWNER)))


def stats(owner: str = "") -> dict:
    where, args = ("WHERE owner=?", (owner,)) if owner else ("", ())
    rows = db._rows(f"SELECT owner, kind, COUNT(*) AS n, SUM(good) AS g, SUM(bad) AS b"
                    f" FROM knowledge {where} GROUP BY owner, kind", args)
    return {"rows": [dict(r) for r in rows],
            "total": sum(int(r["n"]) for r in rows),
            "backends": [dict(r) for r in db._rows(
                "SELECT backend, COUNT(*) AS n FROM knowledge GROUP BY backend", ())]}


def init() -> None:
    db._conn.executescript(SCHEMA)
    db._conn.commit()
