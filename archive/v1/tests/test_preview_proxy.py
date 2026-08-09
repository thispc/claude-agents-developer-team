"""The preview reverse-proxy: a hosted instance gets an openable link for a local app.

Proves the three things that matter: it parses a preview host correctly, it is inert
for every ordinary request (so the dashboard is untouched), and it actually forwards a
request through to a running app and hands the response back — including a path the app
serves at an absolute route, which is the whole point.
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import config, deploy, preview_proxy


@pytest.fixture()
def preview_host(monkeypatch):
    monkeypatch.setattr(config, "PREVIEW_HOST", "152-42-151-175.nip.io")
    return "152-42-151-175.nip.io"


# --- host parsing ----------------------------------------------------------

def test_a_preview_host_maps_to_its_project(preview_host):
    assert preview_proxy.project_for_host("p8.152-42-151-175.nip.io") == 8
    assert preview_proxy.project_for_host("p8.152-42-151-175.nip.io:80") == 8
    assert preview_proxy.project_for_host("p42-staging-task-3.152-42-151-175.nip.io") == 42


def test_the_dashboard_host_is_not_a_preview(preview_host):
    assert preview_proxy.project_for_host("staging.152-42-151-175.nip.io") is None
    assert preview_proxy.project_for_host("devteam.152-42-151-175.nip.io") is None
    assert preview_proxy.project_for_host("p8.someone-else.com") is None
    assert preview_proxy.project_for_host("") is None


def test_inert_when_preview_host_is_unset(monkeypatch):
    monkeypatch.setattr(config, "PREVIEW_HOST", "")
    assert preview_proxy.project_for_host("p8.152-42-151-175.nip.io") is None


# --- the middleware, end to end via the app --------------------------------

def test_a_normal_request_passes_straight_through(client, preview_host):
    """With PREVIEW_HOST set, an ordinary request (dashboard host) is untouched —
    the proxy must never interfere with normal use."""
    assert client.get("/api/health").status_code == 200


def test_a_preview_host_with_no_running_app_says_so(client, preview_host):
    r = client.get("/", headers={"host": "p999.152-42-151-175.nip.io"})
    assert r.status_code == 503
    assert "No preview is running" in r.text


def test_it_proxies_through_to_a_running_app(client, preview_host):
    """The real thing: a request on p<id>.<host> reaches the app on its local port,
    at the app's OWN absolute route, and the response comes back verbatim."""
    class _App(BaseHTTPRequestHandler):
        def do_GET(self):
            body = f"HELLO FROM THE APP at {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _App)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # register it as project 77's running preview, the way deploy_local would
        deploy.RUNNING[deploy._slot(77)] = {
            "proc": type("P", (), {"poll": lambda self: None})(),
            "port": port, "started": 0, "spec": {}, "branch": ""}
        # the app serves an absolute /api path — a subpath proxy could never reach it
        r = client.get("/api/thing", headers={"host": "p77.152-42-151-175.nip.io"})
        assert r.status_code == 200
        assert r.text == "HELLO FROM THE APP at /api/thing"
    finally:
        srv.shutdown()
        deploy.RUNNING.pop(deploy._slot(77), None)
