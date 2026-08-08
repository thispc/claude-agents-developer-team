"""Smoke: the contract's guarantees plus the behaviours worth keeping honest.

In-process and offline. What the CONDUCTOR does with the answers — the composed
notice list, approving locally and deciding remotely, the sub-millisecond log
call — lives in tests/test_watch_service.py; this file proves the SERVICE: auth,
shapes, the committed spec, the ring's arithmetic, the rules, and the first-boot
copy of the four legacy kv keys.
"""

import ast
import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

import watch_service_app as svc        # loaded by conftest under a unique name

SERVICE_DIR = Path(__file__).resolve().parent.parent
TOKEN = {"X-Service-Token": "test-service-token"}

client = TestClient(svc.app)


def _post(rows):
    r = client.post("/logs", json={"rows": rows}, headers=TOKEN)
    assert r.status_code == 200, r.text
    return r.json()


def _code() -> str:
    """The service's source with every docstring and comment removed.

    The pins below say "this lever is not in here", and this file's own prose
    explains at length WHICH levers stayed in the conductor and why — so a naive
    grep matches the explanation and calls it the thing. Stripping the prose is
    what makes the pin mean "the code does not do this" rather than "nobody
    mentions it"."""
    tree = ast.parse((SERVICE_DIR / "app.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and ast.get_docstring(node):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# --- the contract -------------------------------------------------------------

def test_health_is_the_contracts_readiness_shape():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"ok", "service", "db", "checks"}
    assert body["ok"] is True and body["checks"]["db"] is True
    assert body["checks"]["table"] is True
    assert body["service"] == "watch"


def test_the_served_spec_is_the_committed_file_not_a_regeneration():
    """The contract is an artifact somebody reviewed, not the app's opinion of
    itself — otherwise a route that drifted would silently redefine the thing it
    was supposed to be checked against."""
    served = client.get("/openapi.json").json()
    assert served == json.loads((SERVICE_DIR / "openapi.json").read_text())


def test_everything_past_health_needs_the_token():
    for method, path in (("get", "/logs"), ("get", "/logs/stats"), ("get", "/notices"),
                         ("get", "/summary"), ("get", "/auto")):
        assert getattr(client, method)(path).status_code == 401, path
    assert client.post("/logs", json={"rows": []}).status_code == 401
    assert client.post("/auto", json={"on": True}).status_code == 401
    assert client.post("/notices/abc/decide", json={"state": "dismissed"}).status_code == 401
    # ...and a wrong one is refused the same way as none at all.
    assert client.get("/logs", headers={"X-Service-Token": "nope"}).status_code == 401


def test_a_repeated_query_number_is_refused_rather_than_guessed(clean_store):
    """`?window_s=1&window_s=2` used to be answered 200 with the last value.
    A window is the span a notice is derived over, and "whichever you sent last"
    is not an answer anyone can reason about at 3am."""
    r = client.get("/notices?window_s=1&window_s=2", headers=TOKEN)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


# --- the ring -----------------------------------------------------------------

def test_a_row_keeps_its_level_category_event_and_its_own_fields(clean_store):
    _post([{"level": "warn", "cat": "quota", "event": "rate_limited",
            "msg": "sonnet is cooling down", "model": "claude-sonnet-5"}])
    (row,) = client.get("/logs", headers=TOKEN).json()["logs"]
    assert row["level"] == "warn" and row["cat"] == "quota"
    assert row["event"] == "rate_limited"
    assert row["model"] == "claude-sonnet-5", "fields ride along, not glued into the sentence"


def test_an_unknown_category_is_corrected_rather_than_invented(clean_store):
    """The vocabulary is the axis people filter on. A typo that silently created
    a new category would split the very history someone is searching."""
    _post([{"cat": "repair_builder", "event": "x"}])
    assert client.get("/logs", headers=TOKEN).json()["logs"][0]["cat"] == "lifecycle"


def test_a_row_that_would_422_anywhere_else_still_gets_recorded(clean_store):
    """The one lax body in the fleet, on purpose: a caller inside an exception
    handler that passes n="3" where an int was meant must still get its line on
    the record. Losing the evidence about a failure is worse than storing a
    string."""
    _post([{"cat": "http", "event": "boom", "msg": "x", "n": "3", "ok": None}])
    (row,) = client.get("/logs", headers=TOKEN).json()["logs"]
    assert row["n"] == "3" and row["ok"] is None


def test_level_filtering_is_a_floor_not_an_equality(clean_store):
    """Someone asking for warnings is looking for trouble; hiding errors behind a
    stricter filter is the opposite of what they meant."""
    _post([{"level": lv, "cat": "git", "event": ev}
           for lv, ev in (("debug", "a"), ("info", "b"), ("warn", "c"), ("error", "d"))])
    got = lambda qs: [r["event"] for r in                       # noqa: E731
                      client.get(f"/logs?{qs}", headers=TOKEN).json()["logs"]]
    assert got("level=warn") == ["c", "d"]
    assert got("level=debug") == ["a", "b", "c", "d"]
    assert got("cat=git&event=b") == ["b"]


def test_the_free_text_search_reaches_the_fields_not_just_the_sentence(clean_store):
    _post([{"cat": "git", "event": "landed", "msg": "a fix", "sha": "abc1234"},
           {"cat": "git", "event": "other", "msg": "something else"}])
    got = client.get("/logs?q=abc1234", headers=TOKEN).json()["logs"]
    assert [r["event"] for r in got] == ["landed"]


def test_errors_survive_a_chatty_hour(clean_store):
    """The ring is bounded, so a loop that logs every tick would otherwise push
    the one row anyone actually wants out of the record."""
    svc.MAX_ROWS = 10
    _post([{"level": "error", "cat": "sandbox", "event": "escape",
            "msg": "wrote into the live checkout"}])
    _post([{"cat": "sprint", "event": f"noise_{i}"} for i in range(30)])
    logs = client.get("/logs?limit=1000", headers=TOKEN).json()["logs"]
    assert not [r for r in logs if r["event"] == "escape"], "the ring is bounded"
    assert len(logs) == 10
    errs = client.get("/logs?errors_only=true", headers=TOKEN).json()["logs"]
    assert [r["event"] for r in errs] == ["escape"]


def test_stats_turn_a_count_into_a_diagnosis(clean_store):
    _post([{"level": "error", "cat": "sandbox", "event": "escape"},
           {"level": "error", "cat": "sandbox", "event": "escape"},
           {"cat": "git", "event": "landed"}])
    st = client.get("/logs/stats?window_s=3600", headers=TOKEN).json()
    assert st["by_level"]["error"] == 2 and st["by_cat"]["sandbox"] == 2
    assert st["last_error"]["cat"] == "sandbox"
    assert st["categories"]["sandbox"], "the vocabulary travels with the numbers"


# --- dedupe: the thing the ring had to move here for ---------------------------

def test_a_repeated_warning_is_counted_not_repeated(clean_store):
    """A never-die loop that finds the same thing wrong every 20 seconds writes
    180 identical lines an hour, and the record becomes unreadable exactly when
    someone needs it."""
    row = {"level": "warn", "cat": "lifecycle", "event": "lease_held_elsewhere",
           "msg": "standing down", "dedupe_s": 600}
    for _ in range(5):
        _post([row])
    got = client.get("/logs", headers=TOKEN).json()["logs"]
    assert len(got) == 1, "the repetition must collapse"
    assert got[0]["repeats"] == 5, "but the count has to survive"


def test_two_processes_reporting_the_same_fault_collapse_into_one_line(clean_store):
    """The whole reason dedupe lives here and not in the caller. A per-process
    memory gives you one line PER PROCESS, and a fleet then reads as several
    independent incidents where there was one."""
    row = {"level": "warn", "cat": "lifecycle", "event": "lease_held_elsewhere",
           "msg": "standing down", "dedupe_s": 600}
    _post([row])                # "process A"
    _post([row])                # "process B", no shared memory with A
    got = client.get("/logs", headers=TOKEN).json()["logs"]
    assert len(got) == 1 and got[0]["repeats"] == 2


def test_a_later_window_is_news_again(clean_store):
    """Deduping forever would mean a fault that came back a day later never
    reappearing on the record."""
    row = {"level": "warn", "cat": "lifecycle", "event": "lease_held_elsewhere",
           "msg": "standing down", "dedupe_s": 600}
    _post([row])
    svc.helpers.db().execute("UPDATE log_rows SET ts = ts - 1200")
    svc.helpers.db().commit()
    _post([row])
    assert len(client.get("/logs", headers=TOKEN).json()["logs"]) == 2


def test_a_batch_is_one_round_trip_for_many_rows(clean_store):
    out = _post([{"cat": "sprint", "event": f"e{i}"} for i in range(100)])
    assert out == {"stored": 100, "deduped": 0}
    assert len(client.get("/logs?limit=1000", headers=TOKEN).json()["logs"]) == 100


# --- the rules ----------------------------------------------------------------

def test_a_notice_carries_its_evidence_and_a_proposal(clean_store):
    _post([{"level": "error", "cat": "sandbox", "event": "escape",
            "msg": "wrote into the live checkout", "files": "conductor/app/deploy.py"}])
    n = next(x for x in client.get("/notices", headers=TOKEN).json()["notices"]
             if x["kind"] == "sandbox")
    assert n["severity"] == "critical"
    assert "deploy.py" in n["detail"], "a notice has to name the thing it is about"
    assert n["evidence"] and n["evidence"][0]["event"] == "escape"
    assert n["action"] == "pause_repair", "and offer the obvious next move"


def test_a_notice_that_stops_applying_stops_being_reported(clean_store):
    """Derived on read, like project blockers: nothing to clean up, and the list
    can never show a problem that has already gone away."""
    _post([{"level": "error", "cat": "sandbox", "event": "escape", "files": "x.py"}])
    kinds = lambda: {x["kind"] for x in                          # noqa: E731
                     client.get("/notices", headers=TOKEN).json()["notices"]}
    assert "sandbox" in kinds()
    svc.helpers.db().execute("DELETE FROM log_rows")
    svc.helpers.db().commit()
    assert "sandbox" not in kinds()


def test_a_zombie_that_has_been_killed_stops_being_reported(clean_store):
    """A live zombie heartbeats every tick; a dead one stops. Reporting a problem
    that has already been solved is how a monitor loses the right to be
    believed."""
    _post([{"level": "warn", "cat": "lifecycle", "event": "lease_held_elsewhere",
            "msg": "standing down", "holder": 999}])
    svc.helpers.db().execute("UPDATE log_rows SET ts = ?",
                             (time.time() - svc.ZOMBIE_FRESH_S - 60,))
    svc.helpers.db().commit()
    kinds = lambda: [x["kind"] for x in                          # noqa: E731
                     client.get("/notices", headers=TOKEN).json()["notices"]]
    assert "zombie" not in kinds()
    _post([{"level": "warn", "cat": "lifecycle", "event": "lease_held_elsewhere",
            "msg": "standing down now", "holder": 999}])
    assert "zombie" in kinds()


def test_a_dashboard_error_proposes_a_typed_bug(clean_store):
    _post([{"level": "error", "cat": "http", "event": "dashboard_error",
            "msg": "rpLogLine is not defined", "page": "#/improve"}])
    n = next(x for x in client.get("/notices", headers=TOKEN).json()["notices"]
             if x["kind"] == "dashboard")
    assert n["action"] == "file_task" and "next sprint" in n["proposal"]
    assert n["params"]["type"] == "bug" and n["params"]["priority"] == "p2"


def test_the_turns_rule_needs_no_knob_to_make_its_case(clean_store):
    """The pre-P3 prose quoted the current repair_max_turns value. Buying that
    number back would mean giving a detection service the conductor's tuning
    door — a knob read on the path that is supposed to read only log rows."""
    _post([{"level": "error", "cat": "session", "event": "build_died",
            "msg": "ran out of turns"} for _ in range(3)])
    n = next(x for x in client.get("/notices", headers=TOKEN).json()["notices"]
             if x["kind"] == "build_turns")
    assert n["action"] == "set_knob"
    assert n["params"] == {"name": "repair_tasks_per_sprint", "value": 1}
    assert "turn limit is untouched" in n["proposal"]
    assert "tuning" not in _code().lower(), \
        "detection must not depend on the conductor's dials"


def test_a_broken_rule_cannot_blind_the_others(clean_store, monkeypatch):
    def boom(_rows):
        raise ValueError("bad rule")
    monkeypatch.setattr(svc, "RULES", [boom] + list(svc.RULES))
    _post([{"level": "error", "cat": "http", "event": "boom", "msg": "something broke"}])
    kinds = {x["kind"] for x in client.get("/notices", headers=TOKEN).json()["notices"]}
    assert "errors" in kinds
    # ...and the failure itself is on the record rather than swallowed
    assert [r for r in client.get("/logs", headers=TOKEN).json()["logs"]
            if r["event"] == "monitor_rule_failed"]


def test_the_two_local_rules_did_not_come_with_the_others(clean_store):
    """`queue` and `stuck` read the conductor's own repair state, not log rows.
    Moving them would have re-created the cross-owner read the split erased."""
    code = _code()
    assert "_rule_queue" not in code and "_rule_stuck" not in code
    assert "repair:queue" not in code


# --- decisions, and the standing one ------------------------------------------

def test_dismissing_a_notice_silences_that_one_only(clean_store):
    _post([{"level": "error", "cat": "sandbox", "event": "escape", "files": "x.py"},
           {"level": "error", "cat": "http", "event": "boom", "msg": "something else"}])
    body = client.get("/notices", headers=TOKEN).json()
    n = next(x for x in body["notices"] if x["kind"] == "sandbox")
    client.post(f"/notices/{n['fp']}/decide", json={"state": "dismissed"},
                headers=TOKEN).raise_for_status()
    after = client.get("/notices", headers=TOKEN).json()
    kinds = {x["kind"] for x in after["notices"]}
    assert "sandbox" not in kinds and "errors" in kinds
    assert after["decisions"][n["fp"]]["state"] == "dismissed"
    allof = client.get("/notices?include_decided=true", headers=TOKEN).json()
    assert any(x["kind"] == "sandbox" for x in allof["notices"])


def test_the_decisions_map_rides_along_so_the_conductor_needs_one_call(clean_store):
    """The conductor merges two LOCAL rules into this list and has to know which
    of THOSE fingerprints were dismissed. A second round-trip for one small map
    would double the cost of the panel's poll."""
    client.post("/notices/local-fp-123/decide", json={"state": "dismissed"},
                headers=TOKEN).raise_for_status()
    body = client.get("/notices", headers=TOKEN).json()
    assert body["decisions"]["local-fp-123"]["state"] == "dismissed"
    assert set(body) == {"notices", "summary", "decisions", "auto"}


def test_the_standing_decision_is_stored_here_and_acted_on_nowhere(clean_store):
    """`auto` lives beside the decisions because it IS one — a standing answer to
    every future notice of a safe kind. What it authorises runs in the conductor;
    this service holds no lever and the word for one appears nowhere in it."""
    assert client.get("/auto", headers=TOKEN).json() == {"auto": False}
    assert client.post("/auto", json={"on": True}, headers=TOKEN).json() == {"auto": True}
    assert client.get("/auto", headers=TOKEN).json() == {"auto": True}
    assert client.get("/notices", headers=TOKEN).json()["auto"] is True
    code = _code()
    for lever in ("AUTO_SAFE", "ACTIONS", "def approve", "def sweep", "toggle("):
        assert lever not in code, f"{lever} is the conductor's half of the split"


def test_a_summary_counts_what_needs_a_person(clean_store):
    _post([{"level": "error", "cat": "sandbox", "event": "escape", "files": "x.py"},
           {"cat": "quota", "event": "rate_limited", "model": "m"}])
    s = client.get("/summary", headers=TOKEN).json()
    assert s == {"total": 2, "critical": 1, "warning": 0, "needs_approval": 1}


# --- first boot ---------------------------------------------------------------

def test_first_boot_carries_the_four_kv_keys_across_the_seam(tmp_path, monkeypatch):
    """Without the DECISIONS the extraction itself is a notice storm: every
    notice the owner had already dismissed looks new again on the first boot
    after the cutover. The rings come too, or the log view cannot explain the
    night before the move."""
    legacy = tmp_path / "devteam.db"
    con = sqlite3.connect(legacy)
    con.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    con.execute("INSERT INTO kv VALUES (?,?)", ("logs:ring", json.dumps([
        {"ts": 100.0, "level": "error", "cat": "sandbox", "event": "escape",
         "msg": "wrote outside", "files": "x.py", "repeats": 3},
        {"ts": 101.0, "level": "info", "cat": "git", "event": "landed"}])))
    con.execute("INSERT INTO kv VALUES (?,?)", ("logs:errors", json.dumps([
        {"ts": 100.0, "level": "error", "cat": "sandbox", "event": "escape"}])))
    con.execute("INSERT INTO kv VALUES (?,?)", ("monitor:decisions", json.dumps(
        {"abc123": {"state": "dismissed", "ts": 99.0, "note": "known"}})))
    con.execute("INSERT INTO kv VALUES (?,?)", ("monitor:auto", json.dumps(True)))
    con.commit()
    con.close()

    monkeypatch.setattr(svc, "LEGACY_DB_PATH", legacy)
    monkeypatch.setattr(svc.helpers, "DB_PATH", tmp_path / "watch.db")
    monkeypatch.setattr(svc.helpers, "_conn", None)
    svc.init_store()
    assert svc.backfill_from_legacy() == 4
    assert [r["event"] for r in svc.rows()] == ["escape", "landed"]
    assert svc.rows()[0]["repeats"] == 3, "a collapsed count must not be lost"
    assert [r["event"] for r in svc.rows(errors_only=True)] == ["escape"]
    assert svc.decisions()["abc123"]["state"] == "dismissed"
    assert svc.auto_on() is True
    assert svc.backfill_from_legacy() == 0, "the copy runs exactly once"


def test_a_missing_legacy_database_is_a_first_boot_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "LEGACY_DB_PATH", tmp_path / "nothing.db")
    monkeypatch.setattr(svc.helpers, "DB_PATH", tmp_path / "watch2.db")
    monkeypatch.setattr(svc.helpers, "_conn", None)
    svc.init_store()
    assert svc.backfill_from_legacy() == 0
    assert client.get("/health").json()["ok"] is True
