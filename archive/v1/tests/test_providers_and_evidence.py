"""Features: the Gemini/OpenAI credential path, provider-agnostic recruiting, and
the failure-detail the manager judges on.

These are the pieces of docs/IMPROVEMENT_PLAN.md item 2 ("report WHAT failed") and
docs/GEMINI_INTEGRATION.md stage 1 (planner on providers.py).
"""

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

from conftest import make_project, make_task
from app import auth, config, db, manager, planner, providers

# worker.py reads its whole contract from the environment at import time, so it
# needs a minimal one before it can be imported for its pure helpers.
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "worker"))
os.environ.setdefault("TASK_ID", "1")
os.environ.setdefault("PROJECT_ID", "1")
import worker as w  # noqa: E402


# ---- the operator's .env keys must reach the settings layer ----------------

def test_gemini_key_env_alias_is_accepted(monkeypatch):
    """People write GEMINI_KEY in a .env; a var the app never reads looks exactly
    like a broken key."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_KEY", "AIza-from-the-short-name")
    cfg = importlib.reload(config)
    try:
        assert cfg.GEMINI_API_KEY == "AIza-from-the-short-name"
    finally:
        monkeypatch.delenv("GEMINI_KEY", raising=False)
        importlib.reload(config)


def test_root_inherits_operator_provider_keys(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "AIza-x")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-oa")
    s = auth.get_settings(auth.get_user(1))
    assert s["gemini_api_key"] == "AIza-x"
    assert "google" in providers.available(s)
    assert "openai" in providers.available(s)


def test_a_normal_user_never_inherits_them(fresh_db, make_user, monkeypatch):
    """The whole point of per-user credentials: nobody spends the operator's."""
    monkeypatch.setattr(config, "GEMINI_API_KEY", "AIza-operator")
    uid, _ = make_user("mallory")
    s = auth.get_settings(auth.get_user(uid))
    assert "gemini_api_key" not in s
    assert providers.available(s) == []


def test_root_settings_never_carry_empty_placeholders(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    s = auth.get_settings(auth.get_user(1))
    assert all(v for v in s.values()), f"blank values leaked into settings: {s}"


# ---- recruiting works on whatever provider the user holds ------------------

@pytest.mark.asyncio
async def test_planner_uses_gemini_when_that_is_all_the_user_has(monkeypatch):
    seen = {}

    async def fake(provider, model, system, prompt, settings, max_tokens=2000, source=""):
        seen.update(provider=provider, model=model)
        return '[{"role": "propulsion_engineer", "count": 1, "model": "lead"}]'

    monkeypatch.setattr(planner.providers, "complete", fake)
    team = await planner.suggest_team("build a rocket", {"gemini_api_key": "AIza"})
    assert seen["provider"] == "google"
    assert seen["model"].startswith("gemini")
    assert team[0]["role"] == "propulsion_engineer"


@pytest.mark.asyncio
async def test_planner_prefers_claude_when_several_keys_exist(monkeypatch):
    seen = {}

    async def fake(provider, model, system, prompt, settings, max_tokens=2000, source=""):
        seen["provider"] = provider
        return "[]"
    monkeypatch.setattr(planner.providers, "complete", fake)
    await planner.suggest_team("an api", {"gemini_api_key": "A", "claude_oauth_token": "B"})
    assert seen["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_planner_falls_back_to_the_heuristic_with_no_credentials(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("must not call a provider with no key")
    monkeypatch.setattr(planner.providers, "complete", boom)
    team = await planner.suggest_team("build an api with a database", {})
    assert [t["role"] for t in team]        # a usable team, not an empty list


@pytest.mark.asyncio
async def test_a_provider_failure_never_blocks_recruiting(monkeypatch, caplog):
    async def boom(*a, **k):
        raise providers.ProviderError("Gemini returned 429")
    monkeypatch.setattr(planner.providers, "complete", boom)
    team = await planner.suggest_team("a web dashboard", {"gemini_api_key": "A"})
    assert any(t["role"] == "frontend" for t in team)
    # and it must not be silent — an invisible fallback hid a broken key for weeks
    assert "429" in caplog.text


# ---- the judge must be told WHAT failed, not just that something did -------

def test_extract_failures_names_pytest_tests():

    out = ("....F...\n"
           "=================================== FAILURES ===================================\n"
           "E   AssertionError: expected 200, got 500\n"
           "=========================== short test summary info ============================\n"
           "FAILED tests/test_api.py::test_create_order - AssertionError: expected 200\n"
           "1 failed, 12 passed in 4.21s\n")
    got = w.extract_failures(out)
    assert any("test_create_order" in g for g in got)
    assert any("expected 200, got 500" in g for g in got)
    assert w.failure_headline(out) == "1 failed, 12 passed in 4.21s"


def test_extract_failures_handles_other_toolchains():

    assert any("TestOrders" in f for f in w.extract_failures("--- FAIL: TestOrders (0.00s)"))
    assert any("renders" in f for f in w.extract_failures("  ✕ renders the header (4 ms)"))
    assert any("E0432" in f for f in w.extract_failures("error[E0432]: unresolved import"))


def test_extract_failures_stays_quiet_on_a_clean_run():
    """A pattern that fires on ordinary log noise trains the manager to ignore
    the evidence, which is worse than having none."""

    clean = ("Compiling app v0.1.0\nRunning 42 tests\n"
             "test result: ok. 42 passed; 0 failed\n"
             "Note: an error page is rendered for 404s\n")
    assert w.extract_failures(clean) == []


def test_extract_failures_is_deduped_and_bounded():

    noisy = "\n".join(["FAILED tests/test_x.py::test_a - boom"] * 5 +
                      [f"FAILED tests/test_y.py::test_{i} - boom" for i in range(40)])
    got = w.extract_failures(noisy)
    assert len(got) <= 12
    assert len(set(got)) == len(got)


def test_manager_evidence_leads_with_the_failing_test_names(fresh_db):
    p = make_project()
    t = make_task(p)
    db.update_task(t, verification=json.dumps({
        "ran": True, "ok": False, "cmd": "pytest", "exit_code": 1,
        "output": "…lots of log…",
        "headline": "1 failed, 12 passed in 4.21s",
        "failures": ["FAILED tests/test_api.py::test_create_order - expected 200"]}))
    body = manager._with_evidence(db.get_task(t), "I finished the work, all good!")
    assert "test_create_order" in body
    assert "1 failed, 12 passed" in body
    # the evidence must come before the worker's own claim
    assert body.index("test_create_order") < body.index("all good!")


def test_manager_evidence_unchanged_when_verification_passed(fresh_db):
    p = make_project()
    t = make_task(p)
    db.update_task(t, verification=json.dumps(
        {"ran": True, "ok": True, "cmd": "pytest", "exit_code": 0, "output": "ok"}))
    body = manager._with_evidence(db.get_task(t), "done")
    assert "PASSED" in body and "What failed" not in body


# ---- transient provider faults must not silence a seat --------------------

def _resp(status, payload=None, headers=None):
    import httpx as _h
    return _h.Response(status, json=payload or {}, headers=headers or {},
                       request=_h.Request("POST", "https://example.test"))


@pytest.mark.asyncio
async def test_a_503_is_retried_not_surfaced(monkeypatch):
    """Gemini hands out 503s freely on the free tier; one blip used to kill the
    seat permanently for a fault that clears in seconds."""
    import httpx as _h
    calls = {"n": 0}

    async def flaky(model, system, prompt, settings, max_tokens):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _h.HTTPStatusError("busy", request=None,
                                     response=_resp(503, {"error": {"message": "high demand"}}))
        return "recovered"

    monkeypatch.setattr(providers, "_google", flaky)
    monkeypatch.setattr(providers.asyncio, "sleep", lambda *_a, **_k: asyncio_noop())
    got = await providers.complete("google", "m", "s", "p", {"gemini_api_key": "k"})
    assert got == "recovered"
    assert calls["n"] == 3


async def asyncio_noop():
    return None


@pytest.mark.asyncio
async def test_a_404_is_not_retried(monkeypatch):
    """A retired model id is a real error — retrying burns quota for nothing."""
    import httpx as _h
    calls = {"n": 0}

    async def gone(model, system, prompt, settings, max_tokens):
        calls["n"] += 1
        raise _h.HTTPStatusError("gone", request=None, response=_resp(
            404, {"error": {"message": "no longer available to new users"}}))

    monkeypatch.setattr(providers, "_google", gone)
    with pytest.raises(providers.ProviderError) as e:
        await providers.complete("google", "m", "s", "p", {"gemini_api_key": "k"})
    assert calls["n"] == 1
    assert "no longer available" in str(e.value)


def test_google_retry_delay_is_read_from_the_error_body():
    """Google never sends Retry-After; it buries the delay in error.details."""
    assert providers._retry_after(_resp(429, {"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "38s"}]}})) == 38.0
    assert providers._retry_after(_resp(429, {}, {"retry-after": "12"})) == 12.0
    assert providers._retry_after(_resp(429, {})) == 0.0


def test_advertised_gemini_models_are_stable_aliases():
    """gemini-2.5-flash was hard-coded here and had already been retired — still
    listed by ListModels, but generateContent refuses it."""
    ids = [m["id"] for m in providers.PROVIDERS["google"]["models"]]
    assert "gemini-2.5-flash" not in ids
    assert any(i.endswith("-latest") for i in ids)


# ---- role names must not be sliced mid-word -------------------------------

def test_long_role_names_are_cut_at_a_word_boundary():
    assert planner._trim_role("space_qualification_and_link_testing") == \
        "space_qualification_and_link"
    assert planner._trim_role("pointing_acquisition_tracking_engineer") == \
        "pointing_acquisition_tracking"
    assert planner._trim_role("backend") == "backend"


def test_sanitize_produces_usable_role_names():
    out = planner._sanitize(
        [{"role": "Pointing Acquisition Tracking Engineer", "count": 1, "model": "lead"}], "x")
    assert not out[0]["role"].endswith("_en")
    assert " " not in out[0]["role"]


# ---- finding the project's own test command -------------------------------

def _repo(tmp_path, files):
    for rel, body in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    return tmp_path


def test_tests_are_found_at_any_depth(tmp_path):
    """`sim/dsp/test_ber_sim.py` is an ordinary layout, and the one-level glob
    reported the whole project as having no way to check itself."""
    r = _repo(tmp_path, {"sim/dsp/test_ber_sim.py": "def test_x(): pass"})
    got = w.detect_verification(r)
    assert got and got[1] == "pytest"


def test_shallow_tests_still_found(tmp_path):
    assert w.detect_verification(_repo(tmp_path, {"test_a.py": ""}))[1] == "pytest"
    assert w.detect_verification(_repo(tmp_path, {"pkg/thing_test.py": ""}))[1] == "pytest"


def test_a_dependency_s_tests_do_not_count(tmp_path):
    """Walking into node_modules/.venv would let someone else's suite decide
    whether this project is healthy."""
    r = _repo(tmp_path, {"node_modules/dep/test_dep.py": "",
                         ".venv/lib/site-packages/x/test_x.py": "",
                         "README.md": "no tests here"})
    assert w.detect_verification(r) is None


def test_the_repo_s_own_virtualenv_is_preferred(tmp_path):
    """Bare `python -m pytest` fails with 'No module named pytest' when the agent
    installed into a venv — recorded as a FAILED check, blocking good work."""
    r = _repo(tmp_path, {"test_a.py": "", ".venv/bin/python": "#!/bin/sh"})
    (r / ".venv/bin/python").chmod(0o755)
    cmd, _label = w.detect_verification(r)
    assert cmd.startswith(".venv/bin/python"), cmd


def test_system_python_when_there_is_no_venv(tmp_path):
    cmd, _ = w.detect_verification(_repo(tmp_path, {"test_a.py": ""}))
    assert cmd.startswith("python3")


def test_a_project_with_no_verifier_is_reported_as_a_blocker(fresh_db):
    from app import blockers
    p = make_project()
    t = make_task(p, status="done")
    db.update_task(t, verification=json.dumps({"ran": False, "reason": "none declared"}))
    kinds = [b["kind"] for b in blockers.scan(p)]
    assert "unverified" in kinds


def test_a_verified_project_is_not_nagged(fresh_db):
    from app import blockers
    p = make_project()
    t = make_task(p, status="done")
    db.update_task(t, verification=json.dumps({"ran": True, "ok": True, "cmd": "pytest"}))
    assert "unverified" not in [b["kind"] for b in blockers.scan(p)]
