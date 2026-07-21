"""Feature: bring your own inference, not just your own key.

The promise was "your keys, your cloud, your git" and inference was the part
where the provider list was a constant. These tests pin what a user can actually
point the platform at — an OpenAI-compatible server of their own — and, just as
importantly, that an endpoint which does not work says so in Settings rather than
hours later inside a failing project.
"""

import httpx
import pytest

from app import auth, config, credcheck, db, providers


def _mock(handler):
    """Patch httpx.AsyncClient so a call talks to a scripted transport."""
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, *a, **k):
            k["transport"] = transport
            super().__init__(*a, **k)
    return Client


@pytest.fixture()
def http(monkeypatch):
    def _install(handler):
        monkeypatch.setattr(providers.httpx, "AsyncClient", _mock(handler))
    return _install


VLLM = {"id": "cluster", "label": "Our vLLM box", "base_url": "http://vllm.internal/v1",
        "models": ["llama-3.3-70b"]}


def _settings(*endpoints):
    return {providers.CUSTOM_SETTING: list(endpoints)}


# ---- what counts as a configured endpoint ----------------------------------

def test_an_endpoint_without_a_base_url_is_dropped_rather_than_offered():
    """A half-written endpoint in the picker is worse than an absent one: it gets
    chosen and then fails at dispatch."""
    assert providers.normalise_endpoint({"label": "half done"}) is None
    assert providers.custom_endpoints(_settings({"label": "half done"})) == []


def test_a_model_list_may_be_written_as_plain_strings():
    ep = providers.normalise_endpoint(VLLM)
    assert ep["models"] == [{"id": "llama-3.3-70b", "label": "llama-3.3-70b"}]


def test_the_id_is_slugged_from_the_label_when_none_is_given():
    ep = providers.normalise_endpoint({"label": "Our vLLM box!", "base_url": "http://x/v1"})
    assert ep["id"] == "our-vllm-box"


def test_a_custom_endpoint_can_never_shadow_a_built_in_provider():
    """Someone naming their Ollama box 'openai' must not break the real OpenAI."""
    s = _settings({"id": "openai", "base_url": "http://ollama.local/v1"})
    assert "custom:openai" in providers.available(s)
    assert providers.endpoint("openai", s) is None
    assert providers.PROVIDERS["openai"]["models"]


def test_an_endpoint_with_no_key_is_still_available():
    """Ollama and a bare vLLM server authenticate nobody; demanding a key would
    make the commonest self-hosted case impossible to configure."""
    assert providers.available(_settings(VLLM)) == ["custom:cluster"]


def test_the_catalog_shows_the_endpoint_and_never_its_key():
    entry = [p for p in providers.catalog(_settings({**VLLM, "api_key": "sk-secret"}))
             if p["id"] == "custom:cluster"][0]
    assert entry["label"] == "Our vLLM box"
    assert entry["base_url"] == "http://vllm.internal/v1"
    assert "sk-secret" not in repr(entry)


def test_a_custom_endpoint_defaults_to_its_first_model():
    """The built-in lists are ordered by cost, so we skip the top one. A user's
    list is whatever they typed — usually one model — and skipping it serves
    nothing at all."""
    s = _settings({**VLLM, "models": ["big", "small"]})
    assert providers.default_model("custom:cluster", s) == "big"
    assert providers.default_model("openai") == "gpt-5-mini"


# ---- the URL shapes that have to work --------------------------------------

def test_a_version_root_gets_the_chat_path_appended():
    assert providers.chat_url("http://vllm.internal/v1") == \
        "http://vllm.internal/v1/chat/completions"


def test_an_azure_deployment_url_keeps_its_api_version_query():
    """Appending naively puts the path after the query string and produces a 404."""
    got = providers.chat_url(
        "https://x.openai.azure.com/openai/deployments/gpt4o?api-version=2024-10-21")
    assert got == ("https://x.openai.azure.com/openai/deployments/gpt4o"
                   "/chat/completions?api-version=2024-10-21")


def test_a_url_pasted_from_a_curl_example_is_not_doubled():
    assert providers.chat_url("https://api.example.com/v1/chat/completions") == \
        "https://api.example.com/v1/chat/completions"


def test_azure_style_endpoints_send_the_raw_key_under_their_own_header():
    assert providers.auth_headers("k", "api-key") == {"api-key": "k"}
    assert providers.auth_headers("k") == {"Authorization": "Bearer k"}


def test_no_key_means_no_header_at_all():
    """Sending an empty credential is not the same as sending none — some servers
    reject it instead of treating the request as anonymous."""
    assert providers.auth_headers("") == {}


# ---- completing against one ------------------------------------------------

@pytest.mark.asyncio
async def test_a_completion_goes_to_the_configured_server(http):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "hello from the cluster"}}]})

    http(handler)
    out = await providers.complete("custom:cluster", "llama-3.3-70b", "sys", "hi",
                                   _settings({**VLLM, "api_key": "sk-local"}))
    assert out == "hello from the cluster"
    assert seen["url"] == "http://vllm.internal/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-local"


@pytest.mark.asyncio
async def test_a_200_that_is_not_a_completion_names_the_real_problem(http):
    """A base URL pointing at a dashboard rather than an API root is the common
    mistake, and a KeyError tells the user nothing they can act on."""
    http(lambda r: httpx.Response(200, json={"welcome": "nginx"}))
    with pytest.raises(providers.ProviderError) as e:
        await providers.complete("custom:cluster", "llama-3.3-70b", "s", "p",
                                 _settings(VLLM))
    assert "base URL" in str(e.value)


@pytest.mark.asyncio
async def test_an_endpoint_deleted_out_from_under_saved_work_says_so():
    with pytest.raises(providers.ProviderError) as e:
        await providers.complete("custom:gone", "m", "s", "p", _settings(VLLM))
    assert "no provider or endpoint configured" in str(e.value)


# ---- verification, the same way a key is verified --------------------------

def _endpoint_http(monkeypatch, handler):
    monkeypatch.setattr(credcheck.httpx, "AsyncClient", _mock(handler))
    monkeypatch.setattr(providers.httpx, "AsyncClient", _mock(handler))


@pytest.mark.asyncio
async def test_a_working_endpoint_reports_what_it_can_do(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    _endpoint_http(monkeypatch, handler)
    r = await credcheck.check_endpoint(VLLM)
    assert r["ok"] is True
    assert "llama-3.3-70b" in r["detail"]
    assert r["models"] == ["llama-3.3-70b"]


@pytest.mark.asyncio
async def test_a_model_the_server_does_not_have_is_named_in_the_hint(monkeypatch):
    """The commonest misconfiguration is a right server and a wrong model id, and
    "returns 404" is a far worse answer than the list of what it does have."""
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b"}]})
        return httpx.Response(404, json={"error": {"message": "unknown model"}})

    _endpoint_http(monkeypatch, handler)
    r = await credcheck.check_endpoint({**VLLM, "models": ["llama-3.1-70b"]})
    assert r["ok"] is False
    assert "llama-3.3-70b" in r["hint"]


@pytest.mark.asyncio
async def test_a_rejected_key_is_reported_as_a_key_problem(monkeypatch):
    _endpoint_http(monkeypatch, lambda r: httpx.Response(401, json={}))
    r = await credcheck.check_endpoint({**VLLM, "api_key": "wrong"})
    assert r["ok"] is False and "rejected" in r["detail"]
    assert "api-key" in r["hint"]        # the Azure header, worth suggesting


@pytest.mark.asyncio
async def test_an_unreachable_server_blames_the_network_not_the_key(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    _endpoint_http(monkeypatch, handler)
    r = await credcheck.check_endpoint(VLLM)
    assert r["ok"] is False and "could not reach" in r["detail"]


@pytest.mark.asyncio
async def test_a_server_with_no_model_list_is_still_verifiable(monkeypatch):
    """Azure exposes no /models at a deployment path; that is not a failure."""
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    _endpoint_http(monkeypatch, handler)
    r = await credcheck.check_endpoint(VLLM)
    assert r["ok"] is True


# ---- storage: the same rules the keys already follow -----------------------

def test_root_inherits_the_operators_endpoints_and_a_normal_user_does_not(
        fresh_db, make_user, monkeypatch):
    monkeypatch.setattr(config, "CUSTOM_ENDPOINTS", [VLLM])
    assert providers.available(auth.get_settings(auth.get_user(1))) == ["custom:cluster"]
    uid, _ = make_user("mallory")
    assert providers.available(auth.get_settings(auth.get_user(uid))) == []


def test_deleting_an_inherited_endpoint_does_not_bring_it_back(fresh_db, monkeypatch):
    """An operator who removed a decommissioned server must not find it restored
    by the next read of .env."""
    monkeypatch.setattr(config, "CUSTOM_ENDPOINTS", [VLLM])
    assert auth.delete_endpoint(auth.get_user(1), "cluster") is True
    assert providers.custom_endpoints(auth.get_settings(auth.get_user(1))) == []


def test_saving_the_same_endpoint_twice_corrects_it_rather_than_duplicating(fresh_db):
    u = auth.get_user(1)
    auth.save_endpoint(u, VLLM)
    auth.save_endpoint(auth.get_user(1), {**VLLM, "models": ["llama-3.3-70b", "qwen"]})
    eps = providers.custom_endpoints(auth.get_settings(auth.get_user(1)))
    assert len(eps) == 1 and len(eps[0]["models"]) == 2


def test_editing_an_endpoint_without_resending_the_key_keeps_it(fresh_db):
    """The dialog never sends a key back to the browser, so an edit submits a
    blank key field — which must not silently un-authenticate a working server."""
    u = auth.get_user(1)
    auth.save_endpoint(u, {**VLLM, "api_key": "sk-keep"})
    auth.save_endpoint(auth.get_user(1), {**VLLM, "models": ["a", "b"]})
    ep = providers.custom_endpoints(auth.get_settings(auth.get_user(1)))[0]
    assert ep["api_key"] == "sk-keep"


def test_the_dialog_is_told_a_key_is_set_but_never_which(fresh_db):
    auth.save_endpoint(auth.get_user(1), {**VLLM, "api_key": "sk-secret"})
    shown = auth.redacted(auth.get_settings(auth.get_user(1)))["custom_endpoints"]
    assert shown[0]["key_set"] is True
    assert "sk-secret" not in repr(shown)


# ---- through the API -------------------------------------------------------

def test_an_endpoint_can_be_added_listed_and_removed_over_the_api(root_client):
    r = root_client.post("/api/settings/endpoints", json=VLLM)
    assert r.status_code == 200, r.text
    assert [e["id"] for e in r.json()["endpoints"]] == ["cluster"]

    assert root_client.get("/api/settings/endpoints").json()["endpoints"][0]["base_url"] \
        == "http://vllm.internal/v1"

    listed = root_client.get("/api/providers").json()
    assert "custom:cluster" in listed["available"]

    assert root_client.delete("/api/settings/endpoints/cluster").json()["endpoints"] == []
    assert root_client.delete("/api/settings/endpoints/cluster").status_code == 404


def test_an_endpoint_with_no_base_url_is_refused_with_a_reason(root_client):
    r = root_client.post("/api/settings/endpoints", json={"label": "x", "base_url": " "})
    assert r.status_code == 400 and "base URL" in r.json()["detail"]


def test_verify_reaches_the_endpoint_through_the_same_settings_button(
        root_client, monkeypatch):
    root_client.post("/api/settings/endpoints", json=VLLM)

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    _endpoint_http(monkeypatch, handler)
    r = root_client.post("/api/settings/verify",
                         json={"kind": "custom_endpoint", "value": "cluster"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_verifying_an_endpoint_that_does_not_exist_is_a_404(root_client):
    r = root_client.post("/api/settings/verify",
                         json={"kind": "custom_endpoint", "value": "nope"})
    assert r.status_code == 404


def test_a_round_table_seat_on_an_unconfigured_endpoint_names_it(root_client):
    seats = [{"name": n, "provider": "custom:cluster", "model": "llama-3.3-70b"}
             for n in ("A", "B", "C")]
    r = root_client.post("/api/tables", json={"brief": "an idea", "seats": seats})
    assert r.status_code == 400
    assert "custom:cluster" in r.json()["detail"]


def test_a_seat_on_a_configured_endpoint_is_accepted(root_client):
    root_client.post("/api/settings/endpoints", json=VLLM)
    seats = [{"name": n, "provider": "custom:cluster", "model": "llama-3.3-70b"}
             for n in ("A", "B", "C")]
    r = root_client.post("/api/tables", json={"brief": "an idea", "seats": seats})
    assert r.status_code == 200, r.text
    assert db.list_seats(r.json()["id"])[0]["provider"] == "custom:cluster"
