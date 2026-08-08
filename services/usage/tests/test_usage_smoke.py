"""Smoke: the contract's guarantees plus the meter behaviours worth keeping honest.

In-process and offline. What the CONDUCTOR does with the answers — the fail-safe
verdict, the crew's sleep decision — lives in tests/test_usage_service.py; this
file proves the SERVICE: auth, shapes, the committed spec, the arithmetic, the
knob hop, and the first-boot expansion of the legacy kv blob.
"""

import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

import usage_service_app as svc        # loaded by conftest under a unique name

SERVICE_DIR = Path(__file__).resolve().parent.parent
TOKEN = {"X-Service-Token": "test-service-token"}

client = TestClient(svc.app)


# --- the contract -------------------------------------------------------------

def test_health_is_the_contracts_readiness_shape():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"ok", "service", "db", "checks"}
    assert body["ok"] is True and body["checks"]["db"] is True
    assert body["checks"]["table"] is True
    assert body["service"] == "usage"


def test_health_does_not_go_red_because_the_conductor_is_down(clean_store, monkeypatch):
    """Readiness is about THIS service. A probe that fails when a peer restarts
    is how a fleet takes itself down in a ring — and the meter can still answer
    every question with the knobs it already has."""
    monkeypatch.setattr(svc, "CONDUCTOR_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(svc, "TUNING_TRANSPORT", None)
    assert client.get("/health").json()["ok"] is True


def test_health_and_the_spec_need_no_token_but_every_verb_does():
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    for method, path, body in (("post", "/note", {"source": "repair"}),
                               ("get", "/snapshot", None),
                               ("get", "/verdict", None),
                               ("get", "/rows", None)):
        r = getattr(client, method)(path, json=body) if body is not None \
            else client.get(path)
        assert r.status_code == 401, f"{path} answered {r.status_code} without a token"
        r = getattr(client, method)(path, json=body, headers={"X-Service-Token": "wrong"}) \
            if body is not None else client.get(path, headers={"X-Service-Token": "wrong"})
        assert r.status_code == 401, f"{path} accepted a wrong token"


def test_the_served_spec_is_the_committed_file_byte_for_byte():
    committed = json.loads((SERVICE_DIR / "openapi.json").read_text())
    assert client.get("/openapi.json").json() == committed


# --- one INSERT per note: the hazard that caused the extraction ---------------

def test_a_note_is_one_insert_and_nothing_is_read_first(clean_store):
    """The kv blob this replaced did read-append-write under a thread lock, so
    two processes noting at once lost one of them — on the number that decides
    whether the crew may spend the owner's quota. An append-only table cannot."""
    src = svc.__file__ and Path(svc.__file__).read_text()
    body = src.split("def note(", 1)[1].split("\ndef ", 1)[0]
    assert "INSERT INTO usage_rows" in body
    assert "SELECT" not in body.split("_prune", 1)[0], \
        "note() reads before it writes — that is the lost update coming back"


def test_concurrent_notes_all_survive(clean_store):
    """The regression in one line: fifty notes, fifty rows. Under the blob, an
    interleaved read-append-write kept whichever writer finished last."""
    for i in range(50):
        assert client.post("/note", json={"source": "repair", "tok": 1},
                           headers=TOKEN).status_code == 200
    rows = client.get("/rows", headers=TOKEN).json()["rows"]
    assert len(rows) == 50 and sum(r["tok"] for r in rows) == 50


def test_note_records_every_field_and_rows_reads_them_back(clean_store):
    now = time.time()
    client.post("/note", json={"source": "manager", "model": "claude-opus-5",
                               "tok": 4000, "cache": 900_000, "usd": 1.25,
                               "calls": 3, "ts": now - 60}, headers=TOKEN)
    (row,) = client.get("/rows", headers=TOKEN).json()["rows"]
    assert row["source"] == "manager" and row["model"] == "claude-opus-5"
    assert row["tok"] == 4000 and row["cache"] == 900_000
    assert row["usd"] == 1.25 and row["calls"] == 3
    assert abs(row["ts"] - (now - 60)) < 0.01


def test_rows_since_is_a_window_not_the_whole_history(clean_store):
    now = time.time()
    client.post("/note", json={"source": "worker", "tok": 1, "ts": now - 7200}, headers=TOKEN)
    client.post("/note", json={"source": "worker", "tok": 2, "ts": now - 60}, headers=TOKEN)
    assert len(client.get("/rows", headers=TOKEN).json()["rows"]) == 2
    recent = client.get("/rows", params={"since": now - 600}, headers=TOKEN).json()["rows"]
    assert [r["tok"] for r in recent] == [2]


def test_the_source_is_required_and_never_guessed(clean_store):
    """No contextvar, no default, no opinion: this service is TOLD who spent."""
    assert client.post("/note", json={"tok": 1}, headers=TOKEN).status_code == 422
    assert client.post("/note", json={"source": "", "tok": 1}, headers=TOKEN).status_code == 422
    src = Path(svc.__file__).read_text()
    assert "contextvars" not in src, "ambient attribution has no business in a meter"


# --- the arithmetic the sleep decision runs on --------------------------------

def test_the_owners_spend_shrinks_the_crews_allowance(clean_store, knobs):
    now = time.time()
    budget, share = knobs["usage_budget_tokens"], knobs["repair_idle_share"]
    u = client.get("/snapshot", params={"now": now}, headers=TOKEN).json()
    assert u["allowance_tok"] == int(budget * share)
    client.post("/note", json={"source": "worker", "tok": int(budget * share),
                               "ts": now - 60}, headers=TOKEN)
    u = client.get("/snapshot", params={"now": now}, headers=TOKEN).json()
    assert u["allowance_tok"] == 0 and u["idle_frac"] < 1.0


def test_the_crews_own_spend_is_not_the_owners(clean_store):
    """Filed as the owner's, the crew's deliberation would read as 'someone else
    is using the quota' — and the crew would sleep on its own footsteps."""
    now = time.time()
    client.post("/note", json={"source": "repair", "tok": 20_000, "ts": now - 10},
                headers=TOKEN)
    u = client.get("/snapshot", params={"now": now}, headers=TOKEN).json()
    assert u["repair_tok"] == 20_000 and u["owner_tok"] == 0
    assert u["contended"] is False
    v = client.get("/verdict", params={"now": now}, headers=TOKEN).json()
    assert v["ok"] is True


def test_recent_owner_spend_is_contention_and_an_old_one_is_not(clean_store, knobs):
    now = time.time()
    quiet = knobs["repair_yield_quiet_s"]
    client.post("/note", json={"source": "manager", "tok": 4000, "ts": now - 30},
                headers=TOKEN)
    v = client.get("/verdict", params={"now": now}, headers=TOKEN).json()
    assert v["ok"] is False and "your own work" in v["why"]
    assert now < v["wake"] <= now + quiet, "a yield must name a bounded time to check back"

    svc.helpers.db().execute("DELETE FROM usage_rows")
    svc.helpers.db().commit()
    client.post("/note", json={"source": "manager", "tok": 4000,
                               "ts": now - quiet - 60}, headers=TOKEN)
    u = client.get("/snapshot", params={"now": now}, headers=TOKEN).json()
    assert u["contended"] is False
    assert client.get("/verdict", params={"now": now}, headers=TOKEN).json()["ok"] is True


def test_a_spent_share_stops_the_crew_until_the_window_rolls(clean_store, knobs):
    now = time.time()
    share_tok = int(knobs["usage_budget_tokens"] * knobs["repair_idle_share"])
    client.post("/note", json={"source": "repair", "tok": share_tok, "ts": now - 60},
                headers=TOKEN)
    v = client.get("/verdict", params={"now": now}, headers=TOKEN).json()
    assert v["ok"] is False and "share of this window" in v["why"]
    assert v["wake"] > now


def test_rows_outside_the_window_are_not_in_the_picture(clean_store):
    now = time.time()
    client.post("/note", json={"source": "worker", "tok": 999_999,
                               "ts": now - 6 * 3600}, headers=TOKEN)   # window is 5h
    u = client.get("/snapshot", params={"now": now}, headers=TOKEN).json()
    assert u["used_tok"] == 0 and u["by_source"] == {}


def test_cache_tokens_are_counted_apart_from_work(clean_store, knobs):
    """One build reads ~3M cache tokens. Folded into one number, every build
    looks catastrophic and the crew sleeps on a fiction."""
    now = time.time()
    client.post("/note", json={"source": "repair", "tok": 5_000, "cache": 3_000_000,
                               "ts": now - 60}, headers=TOKEN)
    u = client.get("/snapshot", params={"now": now}, headers=TOKEN).json()
    assert u["used_tok"] == 5_000 and u["cache_tok"] == 3_000_000
    assert u["frac"] == round(5_000 / knobs["usage_budget_tokens"], 3)


# --- the knobs are the owner's, read through the conductor --------------------

def test_the_dials_come_from_the_conductor_not_from_a_private_copy(clean_store, knobs,
                                                                     knob_reads):
    now = time.time()
    knobs["repair_idle_share"] = 0.25
    u = client.get("/snapshot", params={"now": now}, headers=TOKEN).json()
    assert u["allowance_tok"] == int(knobs["usage_budget_tokens"] * 0.25)
    assert "repair_idle_share" in knob_reads


def test_the_knobs_are_cached_so_a_snapshot_is_not_four_round_trips(clean_store,
                                                                   knob_reads):
    svc.KNOB_TTL = 30.0
    now = time.time()
    client.get("/snapshot", params={"now": now}, headers=TOKEN)
    first = len(knob_reads)
    client.get("/snapshot", params={"now": now}, headers=TOKEN)
    assert len(knob_reads) == first, "the second snapshot re-asked for every dial"


def test_a_dial_this_service_has_seen_survives_the_conductor_going_away(clean_store, knobs,
                                                                       monkeypatch):
    """STALE-BUT-REAL BEATS DEFAULT-BUT-WRONG. Falling back to the baked default
    would silently re-widen an allowance the owner had narrowed — the one mistake
    a quota meter must not make."""
    now = time.time()
    knobs["repair_idle_share"] = 0.1
    client.get("/snapshot", params={"now": now}, headers=TOKEN)     # sees 0.1

    def _dead(request):
        raise ConnectionError("conductor is down (drill)")
    monkeypatch.setattr(svc, "TUNING_TRANSPORT", __import__("httpx").MockTransport(_dead))
    u = client.get("/snapshot", params={"now": now}, headers=TOKEN).json()
    assert u["allowance_tok"] == int(knobs["usage_budget_tokens"] * 0.1), \
        "the meter fell back to the default instead of the value it had seen"


def test_a_meter_that_never_reached_the_conductor_uses_the_stated_defaults(clean_store,
                                                                           monkeypatch):
    def _dead(request):
        raise ConnectionError("conductor never came up (drill)")
    monkeypatch.setattr(svc, "TUNING_TRANSPORT", __import__("httpx").MockTransport(_dead))
    svc._KNOBS.clear()
    now = time.time()
    u = client.get("/snapshot", params={"now": now}, headers=TOKEN).json()
    assert u["budget_tok"] == svc.KNOB_DEFAULTS["usage_budget_tokens"]
    assert u["window_h"] == round(svc.KNOB_DEFAULTS["usage_window_h"], 2)


# --- first boot: the legacy blob becomes rows ---------------------------------

def _legacy_db(tmp_path, ledger) -> Path:
    p = tmp_path / "devteam.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL)")
    con.execute("INSERT INTO kv (k, v, ts) VALUES ('usage:ledger', ?, 0)",
                (json.dumps(ledger),))
    con.commit()
    con.close()
    return p


def test_first_boot_expands_the_conductors_kv_blob_into_rows(tmp_path, monkeypatch,
                                                             clean_store):
    now = time.time()
    monkeypatch.setattr(svc, "LEGACY_DB_PATH", _legacy_db(tmp_path, [
        {"ts": now - 100, "source": "manager", "model": "m", "tok": 10, "cache": 5,
         "usd": 0.5, "calls": 1},
        {"ts": now - 50, "source": "repair", "model": "m", "tok": 20, "calls": 2},
    ]))
    svc.helpers.kv_set("backfilled_from", "")       # pretend it never ran
    svc.helpers.db().execute("DELETE FROM kv WHERE key='backfilled_from'")
    svc.helpers.db().commit()
    assert svc.backfill_from_legacy() == 2
    rows = client.get("/rows", headers=TOKEN).json()["rows"]
    assert [r["source"] for r in rows] == ["manager", "repair"]
    assert [r["tok"] for r in rows] == [10, 20]
    # and exactly once, whatever else happens
    assert svc.backfill_from_legacy() == 0


def test_rows_from_before_the_work_cache_split_arrive_as_zero_tokens(tmp_path, monkeypatch,
                                                                     clean_store):
    """Their `tokens` field summed all four kinds — one build's 3M cache reads
    inside it — so reading them as work tokens pinned the meter at 100% and put
    the crew to sleep on a fiction. They count as CALLS, which is what the
    session-count backstop is for, and an honest zero beats a number we know is
    wrong."""
    now = time.time()
    monkeypatch.setattr(svc, "LEGACY_DB_PATH", _legacy_db(tmp_path, [
        {"ts": now - 60, "source": "repair", "model": "m", "usd": 1.7,
         "tokens": 3_018_222, "calls": 1}]))
    svc.helpers.db().execute("DELETE FROM kv WHERE key='backfilled_from'")
    svc.helpers.db().commit()
    assert svc.backfill_from_legacy() == 1
    u = client.get("/snapshot", params={"now": now}, headers=TOKEN).json()
    assert u["used_tok"] == 0 and u["frac"] == 0
    assert u["calls"] == 1, "still visible as a call — that is what the backstop counts"
    assert u["usd"] == 1.7


def test_no_legacy_database_is_not_an_error(tmp_path, monkeypatch, clean_store):
    monkeypatch.setattr(svc, "LEGACY_DB_PATH", tmp_path / "nothing-here.db")
    svc.helpers.db().execute("DELETE FROM kv WHERE key='backfilled_from'")
    svc.helpers.db().commit()
    assert svc.backfill_from_legacy() == 0
    assert client.get("/health").json()["ok"] is True
