"""Faults the platform noticed about itself, as objects it can act on.

Everything needed for this was already being recorded and none of it was being
read. Dispatch errors, stalled workers, rate limits, provider failures and
JavaScript errors from the dashboard all landed in the events table, and the
events table is a feed — visible only to someone already watching it, and
scrolling past at the speed of a running project. A fault that happened forty
times over a weekend looked exactly like a fault that happened once, because
neither was ever counted.

`notify` fixed half of this by filing a GitHub issue per distinct fault. That
gets word out, but an issue is a message, not a thing the platform holds: it
cannot be ranked against other faults, it does not know it is the same problem as
the one from last Tuesday, and nothing in the system can decide it is now bad
enough to be worth a sprint. A finding is that missing object.

Three deliberate choices:

- **Frequency is the signal, severity is the ceiling.** A crash that happened
  once is a curiosity; the same crash forty times is a defect. But frequency
  alone would rank a noisy warning above a rare data-loss bug, so severity sets
  the band and frequency ranks within it.
- **Decay.** A fault that stopped happening a week ago should sink, even if it
  was frequent then, because it was probably fixed by something. Without decay
  the top of the list is a museum.
- **Nothing starts work on its own.** Scanning, scoring and ranking are free and
  happen on a schedule. Turning a finding into a sprint spends real money and is
  gated on an explicit threshold — and on the same triage rules that already
  decide how much independence any change gets.
"""

import hashlib
import json
import re
import time
from typing import Any

from . import bus, config, db, tuning

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,                       -- what sort of fault
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'warning', -- critical | warning | info
    count INTEGER NOT NULL DEFAULT 1,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',      -- open | ticketed | fixed | dismissed
    project_id INTEGER,                       -- where it was seen
    task_id INTEGER,
    issue_number INTEGER,                     -- once it became a ticket
    repair_project INTEGER                    -- the self-repair project working it
);
CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status, last_seen);
"""

# Which recorded events are evidence of a fault, and how bad each is on its face.
# Deliberately narrow: an event kind added here that fires in normal operation
# would bury everything real under noise, and a scanner nobody trusts gets
# ignored, which is worse than not having one.
WATCHED: dict[str, tuple[str, str]] = {
    "dispatch_error":   ("critical", "a worker could not be launched"),
    "worker_stalled":   ("warning", "a worker went silent and was killed"),
    "client_error":     ("warning", "the dashboard raised a JavaScript error"),
    "http_error":       ("warning", "a request failed"),
    "provider_error":   ("warning", "a model provider returned an error"),
    "repo_error":       ("warning", "a repository could not be prepared"),
    "manager_crashed":  ("critical", "a manager session died"),
    "self_update_failed": ("critical", "the platform could not update itself"),
}

# Rate limits are excluded on purpose. They are frequent, they are not defects,
# and the platform already handles them with cooldowns and escalation. Listing
# them would put them permanently at the top of a list meant for things worth
# fixing.

SEVERITY_BAND = {"critical": 1000.0, "warning": 100.0, "info": 10.0}
HALF_LIFE = 3 * 86400     # a fault silent for three days scores half as much


def init() -> None:
    db._conn.executescript(SCHEMA)
    db._conn.commit()


def _fingerprint(kind: str, detail: str) -> str:
    """What makes two faults the same one.

    Numbers are stripped before hashing. Stack traces and error strings differ by
    task id, line number, port and timestamp between otherwise identical failures,
    and a fingerprint that kept them would file a fresh finding for every single
    occurrence — which is exactly the counting problem this exists to solve.
    """
    head = (detail or "").strip().splitlines()[0][:200] if detail else ""
    normalised = re.sub(r"\d+", "#", head).lower()
    return hashlib.sha256(f"{kind}|{normalised}".encode()).hexdigest()[:12]


def record(kind: str, detail: str, *, severity: str = "", project_id: int | None = None,
           task_id: int | None = None, title: str = "") -> dict | None:
    """Note one occurrence. Returns the finding, or None if this kind is not watched."""
    if kind not in WATCHED and not severity:
        return None
    band, label = WATCHED.get(kind, (severity or "warning", kind))
    sev = severity or band
    fp = _fingerprint(kind, detail)
    now = time.time()
    existing = db._rows("SELECT * FROM findings WHERE fingerprint=?", (fp,))
    if existing:
        row = existing[0]
        # A recurrence of something marked fixed is more interesting than a new
        # fault: it means a repair did not hold, and silently re-opening it as
        # though it were fresh would hide that.
        status = "open" if row["status"] in ("fixed", "dismissed") else row["status"]
        db._execute(
            "UPDATE findings SET count=count+1, last_seen=?, status=? WHERE id=?",
            (now, status, row["id"]))
        if row["status"] == "fixed":
            bus.emit(0, None, "system", "finding_reopened",
                     {"title": row["title"], "count": row["count"] + 1})
        return db._rows("SELECT * FROM findings WHERE id=?", (row["id"],))[0]

    head = (detail or "").strip().splitlines()[0][:120] if detail else label
    db._execute(
        "INSERT INTO findings (fingerprint, kind, title, detail, severity, count, "
        "first_seen, last_seen, project_id, task_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (fp, kind, (title or f"{label}: {head}")[:200], (detail or "")[:4000], sev,
         1, now, now, project_id, task_id))
    return db._rows("SELECT * FROM findings WHERE fingerprint=?", (fp,))[0]


def score(finding: dict, now: float | None = None) -> float:
    """How much this deserves attention right now.

    Severity sets the band and frequency ranks within it, so a noisy warning can
    never outrank a rare critical fault. Recency decays, because something that
    stopped happening was probably fixed by something else, and a list topped by
    resolved history is a list nobody reads twice.
    """
    now = now or time.time()
    band = SEVERITY_BAND.get(finding["severity"], 10.0)
    age = max(0.0, now - finding["last_seen"])
    decay = 0.5 ** (age / HALF_LIFE)
    # Logarithmic in count: the difference between 1 and 10 occurrences matters
    # far more than between 100 and 1000, and linear scaling lets one runaway loop
    # own the top of the list forever.
    from math import log
    return band * (1 + log(1 + finding["count"])) * decay


def ranked(status: str = "open", limit: int = 50) -> list[dict]:
    now = time.time()
    rows = db._rows("SELECT * FROM findings WHERE status=? ORDER BY last_seen DESC LIMIT 500",
                    (status,))
    out = [{**r, "score": round(score(r, now), 1)} for r in rows]
    out.sort(key=lambda f: -f["score"])
    return out[:limit]


def get(finding_id: int) -> dict | None:
    rows = db._rows("SELECT * FROM findings WHERE id=?", (finding_id,))
    return rows[0] if rows else None


def set_status(finding_id: int, status: str, **fields: Any) -> None:
    cols = ", ".join(["status=?"] + [f"{k}=?" for k in fields])
    db._execute(f"UPDATE findings SET {cols} WHERE id=?",
                (status, *fields.values(), finding_id))


def scan(since: float | None = None) -> dict[str, Any]:
    """Read the event log for faults nobody has counted yet.

    Reads rather than hooks. Hooking every emit site would mean touching a dozen
    modules and would still miss anything added later; the events table already
    has all of it, and a scanner that works off stored rows also picks up whatever
    happened while it was not running.
    """
    last = since if since is not None else db.kv_get("findings_scanned_to", 0.0)
    now = time.time()
    rows = db._rows(
        "SELECT * FROM events WHERE ts > ? AND kind IN (%s) ORDER BY id LIMIT 5000"
        % ",".join("?" * len(WATCHED)),
        (last, *WATCHED.keys()))
    seen = 0
    for e in rows:
        # Payloads are whatever the emitting site passed — a string, a dict, or
        # occasionally a list. Anything unrecognised is stringified rather than
        # skipped: a scanner that drops the events it cannot parse is silently
        # blind to exactly the faults with the weirdest shape.
        detail = str(e["payload"])
        try:
            parsed = json.loads(e["payload"])
            if isinstance(parsed, str):
                detail = parsed
            elif isinstance(parsed, dict):
                detail = (parsed.get("detail") or parsed.get("note")
                          or parsed.get("reason") or json.dumps(parsed, sort_keys=True))
            else:
                detail = json.dumps(parsed, sort_keys=True)
        except (json.JSONDecodeError, TypeError):
            pass
        record(e["kind"], str(detail), project_id=e["project_id"], task_id=e["task_id"])
        seen += 1
    db.kv_set("findings_scanned_to", now)
    top = ranked(limit=5)
    if seen:
        bus.emit(0, None, "system", "self_scan",
                 {"events": seen, "open": len(ranked(limit=500)),
                  "top": [f["title"] for f in top[:3]]})
    return {"events_read": seen, "scanned_to": now, "top": top}


def worth_a_sprint() -> list[dict]:
    """Which open findings clear the bar for spending money on.

    The threshold is a knob rather than a constant precisely because the right
    value depends on how noisy a given deployment is, and nobody can know that in
    advance — including me.
    """
    floor = float(tuning.get("repair_score_floor"))
    return [f for f in ranked() if f["score"] >= floor]


def summary() -> dict[str, Any]:
    """What the platform currently knows is wrong with it, for the UI."""
    counts = {s: len(db._rows("SELECT id FROM findings WHERE status=?", (s,)))
              for s in ("open", "ticketed", "fixed", "dismissed")}
    return {
        "counts": counts,
        "open": ranked(limit=20),
        "actionable": [f["id"] for f in worth_a_sprint()],
        "last_scan": db.kv_get("findings_scanned_to", 0.0),
        "floor": float(tuning.get("repair_score_floor")),
    }
