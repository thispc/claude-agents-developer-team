"""Smoke: the contract's guarantees plus the knowledge behaviours worth keeping honest.

In-process and offline. Retrieval quality itself is covered in depth by the
conductor suite against the same body (tests/test_knowledge.py, until commit B
moves those assertions here); this file proves the SERVICE — auth, shapes, the
committed spec, the verbs over HTTP, and the first-boot legacy copy.
"""

import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

import knowledge_service_app as svc     # loaded by conftest under a unique name

SERVICE_DIR = Path(__file__).resolve().parent.parent
TOKEN = {"X-Service-Token": "test-service-token"}

client = TestClient(svc.app)


def test_health_is_the_contracts_readiness_shape():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"ok", "service", "db", "checks"}
    assert body["ok"] is True and body["checks"]["db"] is True
    assert body["checks"]["table"] is True
    assert body["service"] == "knowledge"


def test_health_and_the_spec_need_no_token_but_every_verb_does():
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    for method, path, body in (("post", "/recall", {"owner": "a", "query": "q"}),
                               ("post", "/remember", {"owner": "a", "cue": "c", "says": "s"}),
                               ("post", "/reinforce", {"id": 1, "outcome": "good"}),
                               ("post", "/forget", {"owner": "a"}),
                               ("get", "/stats", None),
                               ("post", "/tokens", {"text": "x"})):
        r = getattr(client, method)(path, json=body) if body is not None \
            else client.get(path)
        assert r.status_code == 401, f"{path} answered {r.status_code} without a token"
        r = getattr(client, method)(path, json=body, headers={"X-Service-Token": "wrong"}) \
            if body is not None else client.get(path, headers={"X-Service-Token": "wrong"})
        assert r.status_code == 401, f"{path} accepted a wrong token"


def test_the_served_spec_is_the_committed_file_byte_for_byte():
    committed = json.loads((SERVICE_DIR / "openapi.json").read_text())
    assert client.get("/openapi.json").json() == committed


def test_remember_recall_roundtrip_and_every_hit_explains_itself(clean_store):
    r = client.post("/remember", json={
        "owner": "a1", "cue": "the build failed with ImportError: no module named app",
        "says": "an ImportError here means the venv symlink, not the code",
        "sig": "error:ImportError", "good": 2}, headers=TOKEN)
    assert r.status_code == 200 and r.json()["id"] > 0
    r = client.post("/recall", json={"owner": "a1", "query": "ImportError building the app",
                                     "k": 3}, headers=TOKEN)
    hits = r.json()["hits"]
    assert hits and "venv symlink" in hits[0]["says"]
    assert set(hits[0]["why"]) >= {"similarity", "shared_terms", "relevance",
                                   "held_up", "matched"}


def test_recall_stays_silent_on_something_it_knows_nothing_about(clean_store):
    client.post("/remember", json={"owner": "a1", "cue": "HTTP 505 from the billing host",
                                   "says": "a 505 there is the staging env"}, headers=TOKEN)
    r = client.post("/recall", json={"owner": "a1",
                                     "query": "what is the capital of France"}, headers=TOKEN)
    assert r.json()["hits"] == []


def test_remember_upserts_on_the_four_part_key(clean_store):
    body = {"owner": "a1", "cue": "same situation", "says": "first lesson",
            "sig": "s", "good": 1}
    id1 = client.post("/remember", json=body, headers=TOKEN).json()["id"]
    body["says"], body["good"] = "second lesson", 1
    id2 = client.post("/remember", json=body, headers=TOKEN).json()["id"]
    assert id1 == id2
    hits = client.post("/recall", json={"owner": "a1", "query": "same situation"},
                       headers=TOKEN).json()["hits"]
    assert hits[0]["says"] == "second lesson" and hits[0]["good"] == 2, \
        "an upsert must replace the lesson and ACCUMULATE the record"


def test_reinforce_is_the_only_thing_that_moves_confidence(clean_store):
    client.post("/remember", json={"owner": "a1", "cue": "HTTP 505 from billing",
                                   "says": "staging env", "good": 3}, headers=TOKEN)
    hit = client.post("/recall", json={"owner": "a1", "query": "505 from billing", "k": 1},
                      headers=TOKEN).json()["hits"][0]
    assert client.post("/reinforce", json={"id": hit["id"], "outcome": "bad"},
                       headers=TOKEN).json()["ok"] is True
    after = client.post("/recall", json={"owner": "a1", "query": "505 from billing", "k": 1},
                        headers=TOKEN).json()["hits"][0]
    assert after["confidence"] < hit["confidence"]
    # an unknown outcome is a recorded no-op, not a 500
    assert client.post("/reinforce", json={"id": hit["id"], "outcome": "meh"},
                       headers=TOKEN).json()["ok"] is False


def test_forget_by_row_sig_and_owner(clean_store):
    for sig in ("s1", "s2", ""):
        client.post("/remember", json={"owner": "a1", "cue": f"thing {sig or 'x'}",
                                       "says": "lesson", "sig": sig}, headers=TOKEN)
    hits = client.post("/recall", json={"owner": "a1", "query": "thing", "k": 5},
                       headers=TOKEN).json()["hits"]
    rid = hits[0]["id"]
    assert client.post("/forget", json={"owner": "a1", "row_id": rid},
                       headers=TOKEN).json()["removed"] == 1
    assert client.post("/forget", json={"owner": "a1", "sig": "s1"},
                       headers=TOKEN).json()["removed"] == 1
    assert client.post("/forget", json={"owner": "a1"}, headers=TOKEN).json()["removed"] == 1
    assert client.get("/stats", params={"owner": "a1"}, headers=TOKEN).json()["total"] == 0


def test_it_prunes_what_never_helped(clean_store, monkeypatch):
    """A knowledge base that only grows is a landfill."""
    monkeypatch.setattr(svc, "MAX_PER_OWNER", 5)
    for i in range(12):
        client.post("/remember", json={"owner": "a2", "cue": f"a thing number {i}",
                                       "says": f"lesson {i}"}, headers=TOKEN)
    assert client.get("/stats", params={"owner": "a2"}, headers=TOKEN).json()["total"] <= 5


def test_stats_names_owners_kinds_and_backends(clean_store):
    client.post("/remember", json={"owner": "a1", "cue": "one thing", "says": "lesson"},
                headers=TOKEN)
    body = client.get("/stats", headers=TOKEN).json()
    assert body["total"] == 1
    assert body["rows"][0]["owner"] == "a1" and body["rows"][0]["kind"] == "belief"
    assert body["backends"][0]["backend"] == svc.LOCAL


def test_the_tokenizer_is_contract_now(clean_store):
    """The lifeworld's leak-checks and recall must agree on what a word is — that
    agreement used to be a private reach-in (ports → knowledge._tokens); it is an
    endpoint now, and the exact behaviours the conductor suite pinned still hold."""
    r = client.post("/tokens", json={"text": "the build is on the host"}, headers=TOKEN)
    assert r.json()["tokens"] == ["build", "host"]
    assert client.post("/tokens", json={"text": "127.0.0.1:8787"},
                       headers=TOKEN).json()["tokens"] == ["127", "8787"]


def test_a_vector_is_never_compared_across_backends():
    """A hashed vector against a neural one is not a worse answer, it is a
    meaningless one — a mismatched row is re-embedded, never skipped."""
    import inspect
    src = inspect.getsource(svc.recall)
    assert 'r["backend"] == backend' in src
    assert 'embed_local(r["cue"])' in src


# --- the first-boot legacy copy ----------------------------------------------

def _legacy_db(tmp_path, table: str) -> Path:
    p = tmp_path / "devteam.db"
    con = sqlite3.connect(p)
    con.executescript(svc.K_SCHEMA.replace("knowledge", table))
    v = svc.embed_local("the sandbox port was already taken")
    con.execute(f"INSERT INTO {table} (owner, kind, sig, cue, says, payload, backend,"
                " dim, vec, good, bad, used, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("global", "episode", "", "the sandbox port was already taken",
                 "this failed: port collision", "{}", svc.LOCAL, svc.DIM,
                 svc._blob(v), 0, 1, 0, time.time()))
    con.commit()
    con.close()
    return p


def test_first_boot_copies_the_legacy_rows_once(clean_store, tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "LEGACY_DB_PATH", _legacy_db(tmp_path, "knowledge"))
    svc.helpers.db().execute("DELETE FROM kv WHERE key='backfilled_from'")
    svc.helpers.db().commit()
    assert svc.backfill_from_legacy() == 1
    marker = json.loads(svc.helpers.kv_get("backfilled_from"))
    assert marker["rows"] == 1 and marker["table"] == "knowledge"
    hits = client.post("/recall", json={"owner": "global",
                                        "query": "sandbox port already taken"},
                       headers=TOKEN).json()["hits"]
    assert hits and "port collision" in hits[0]["says"]
    # the marker makes the copy exactly-once, even into an emptied table
    svc.helpers.db().execute("DELETE FROM knowledge")
    svc.helpers.db().commit()
    assert svc.backfill_from_legacy() == 0


def test_first_boot_finds_the_renamed_table_too(clean_store, tmp_path, monkeypatch):
    """The conductor's URL-mode shim renames knowledge → knowledge_legacy on its
    first boot, and the two processes start in no guaranteed order."""
    monkeypatch.setattr(svc, "LEGACY_DB_PATH", _legacy_db(tmp_path, "knowledge_legacy"))
    svc.helpers.db().execute("DELETE FROM kv WHERE key='backfilled_from'")
    svc.helpers.db().commit()
    assert svc.backfill_from_legacy() == 1
    assert json.loads(svc.helpers.kv_get("backfilled_from"))["table"] == "knowledge_legacy"
    svc.helpers.db().execute("DELETE FROM kv WHERE key='backfilled_from'")
    svc.helpers.db().commit()


def test_a_populated_store_is_never_backfilled_over(clean_store, tmp_path, monkeypatch):
    client.post("/remember", json={"owner": "a1", "cue": "already here", "says": "kept"},
                headers=TOKEN)
    monkeypatch.setattr(svc, "LEGACY_DB_PATH", _legacy_db(tmp_path, "knowledge"))
    svc.helpers.db().execute("DELETE FROM kv WHERE key='backfilled_from'")
    svc.helpers.db().commit()
    assert svc.backfill_from_legacy() == 0
    assert client.get("/stats", headers=TOKEN).json()["total"] == 1
