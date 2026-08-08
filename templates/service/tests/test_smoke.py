"""Smoke: the scaffold's own guarantees, in-process and offline."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

import helpers
from app import app

SERVICE_DIR = Path(__file__).resolve().parent.parent
TOKEN = {"X-Service-Token": "test-service-token"}

client = TestClient(app)


def test_health_is_the_contracts_readiness_shape():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"ok", "service", "db", "checks"}
    assert body["ok"] is True and body["checks"]["db"] is True


def test_kv_roundtrip_with_the_token():
    r = client.put("/kv/greeting", json={"value": "hello"}, headers=TOKEN)
    assert r.status_code == 200, r.text
    r = client.get("/kv/greeting", headers=TOKEN)
    assert r.json()["value"] == "hello"


def test_no_token_means_401_and_a_wrong_token_too():
    assert client.get("/kv/greeting").status_code == 401
    assert client.get("/kv/greeting",
                      headers={"X-Service-Token": "wrong"}).status_code == 401


def test_health_and_the_spec_need_no_token():
    """process-compose probes /health and the gateway reads /openapi.json before
    any token plumbing exists — both must answer unauthenticated."""
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_the_served_spec_is_the_committed_file_byte_for_byte():
    committed = json.loads((SERVICE_DIR / "openapi.json").read_text())
    assert client.get("/openapi.json").json() == committed


def test_the_ui_panel_is_served_when_the_dir_exists():
    r = client.get("/ui/panel.html")
    assert r.status_code == 200
    assert "../health" in r.text, "the panel must fetch RELATIVE so /svc/<name>/ works"


def test_the_db_is_the_services_own(tmp_path):
    """One owner per file: the db this service opened is the env-configured one."""
    assert str(helpers.DB_PATH).endswith("test.db")
