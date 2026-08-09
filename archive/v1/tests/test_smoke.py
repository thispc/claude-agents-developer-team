def test_health_and_login(root_client):
    r = root_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True

def test_root_sees_no_projects_on_fresh_db(root_client):
    r = root_client.get("/api/projects")
    assert r.status_code == 200
    assert r.json() == []
