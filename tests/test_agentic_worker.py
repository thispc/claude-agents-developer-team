"""The worker loop for providers that do not ship an agent.

"Bring your own AI keys" was true for planning and false for building: every
worker was the Claude Agent SDK. These cover the mechanics of the replacement
loop — tool dispatch, the turn budget, arguments a model got wrong, a tool that
blows up, and the final-summary contract the conductor reads — against a fake
provider, because the suite runs with every credential blanked.
"""

import copy
import json
import os
import sys
from pathlib import Path

import pytest

from conftest import make_project, make_task
from app import db, launcher, team

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "worker"))
os.environ.setdefault("TASK_ID", "1")
os.environ.setdefault("PROJECT_ID", "1")
import agentic  # noqa: E402


class FakeEngine:
    """A provider that says what the test tells it to say.

    Records what it was asked and what it was told, so a test can assert on the
    conversation rather than on the loop's internals.
    """

    def __init__(self, script, repeat_last=False):
        self.script = list(script)
        self.repeat_last = repeat_last
        self._last = None
        self.started = None
        self.results = []
        self.turns = 0
        self.tools_offered = None

    def start(self, system, prompt):
        self.started = (system, prompt)

    async def turn(self, tools):
        self.tools_offered = tools
        self.turns += 1
        if self.script:
            self._last = self.script.pop(0)
        elif not self.repeat_last:
            return agentic.Reply(text="(nothing scripted)")
        return self._last

    def record_result(self, call, output):
        self.results.append((call.name, output))


def _call(name, args, cid="c1"):
    return agentic.ToolCall(id=cid, name=name, args=args, raw=json.dumps(args))


async def _run(tmp_path, script, max_turns=10, repeat_last=False, events=None):
    engine = FakeEngine(script, repeat_last=repeat_last)
    emit = (lambda kind, payload: events.append((kind, payload))) if events is not None else None
    text, cost = await agentic.run_session(
        provider="openai", model="gpt-5-mini", key="unused", system="sys",
        prompt="do the thing", repo_dir=tmp_path, max_turns=max_turns,
        emit=emit, engine=engine)
    return engine, text, cost


async def test_the_session_ends_on_the_first_reply_with_no_tool_call(tmp_path):
    """That message is the report the manager reads — the same contract the Claude
    path fulfils, so nothing downstream has to know which engine ran."""
    engine, text, _ = await _run(tmp_path, [
        agentic.Reply(calls=[_call("write_file", {"path": "a.txt", "content": "hi"})]),
        agentic.Reply(text="Added a.txt. Verified by reading it back."),
    ])
    assert text == "Added a.txt. Verified by reading it back."
    assert (tmp_path / "a.txt").read_text() == "hi"
    assert engine.turns == 2


async def test_tool_calls_actually_change_the_clone(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VERSION = '1'\nprint(VERSION)\n")

    engine, _, _ = await _run(tmp_path, [
        agentic.Reply(calls=[_call("read_file", {"path": "src/app.py"})]),
        agentic.Reply(calls=[_call("edit_file", {"path": "src/app.py",
                                                 "old": "'1'", "new": "'2'"})]),
        agentic.Reply(calls=[_call("search", {"pattern": "VERSION = "})]),
        agentic.Reply(calls=[_call("list_files", {"pattern": "**/*.py"})]),
        agentic.Reply(calls=[_call("run_shell", {"command": "echo built"})]),
        agentic.Reply(text="done"),
    ])
    outputs = dict(engine.results)
    assert "VERSION = '1'" in outputs["read_file"]
    assert (tmp_path / "src" / "app.py").read_text().startswith("VERSION = '2'")
    assert "src/app.py:1" in outputs["search"]
    assert "src/app.py" in outputs["list_files"]
    assert "exit code 0" in outputs["run_shell"] and "built" in outputs["run_shell"]


async def test_an_edit_that_does_not_match_changes_nothing_and_says_so(tmp_path):
    """A silent no-op edit is how an agent ends up reporting work it never did."""
    (tmp_path / "f.py").write_text("a = 1\na = 1\n")
    engine, _, _ = await _run(tmp_path, [
        agentic.Reply(calls=[_call("edit_file", {"path": "f.py", "old": "zzz", "new": "x"})]),
        agentic.Reply(calls=[_call("edit_file", {"path": "f.py", "old": "a = 1", "new": "x"})]),
        agentic.Reply(text="done"),
    ])
    assert (tmp_path / "f.py").read_text() == "a = 1\na = 1\n"
    assert "not in the file" in engine.results[0][1]
    assert "appears 2 times" in engine.results[1][1]


async def test_arguments_that_are_not_valid_json_cost_a_turn_not_the_session(tmp_path):
    """Small models emit broken JSON regularly. Telling the model is one turn;
    raising would throw away everything already written into the clone."""
    broken = agentic.ToolCall(id="c1", name="write_file", args=None, raw="{'path': oops")
    engine, text, _ = await _run(tmp_path, [
        agentic.Reply(calls=[broken]),
        agentic.Reply(text="recovered"),
    ])
    assert "not valid JSON" in engine.results[0][1]
    assert text == "recovered"


async def test_a_tool_that_raises_comes_back_as_a_message_the_model_can_act_on(tmp_path):
    engine, text, _ = await _run(tmp_path, [
        # a directory where a file is expected: the read blows up inside the tool
        agentic.Reply(calls=[_call("read_file", {})]),
        agentic.Reply(calls=[_call("search", {"pattern": "(unclosed"})]),
        agentic.Reply(text="fine"),
    ])
    assert "read_file failed: ValueError" in engine.results[0][1]
    assert "not a valid regular expression" in engine.results[1][1]
    assert text == "fine"


async def test_a_tool_the_model_invented_is_answered_with_the_real_list(tmp_path):
    engine, _, _ = await _run(tmp_path, [
        agentic.Reply(calls=[_call("delete_everything", {})]),
        agentic.Reply(text="ok"),
    ])
    assert "no tool called 'delete_everything'" in engine.results[0][1]
    assert "run_shell" in engine.results[0][1]


async def test_the_turn_limit_fails_in_the_words_the_launcher_retries_on(tmp_path):
    """The launcher greps the report for this phrase to give a retry a bigger
    budget instead of a different model — see conductor/app/launcher.py."""
    with pytest.raises(agentic.AgenticError) as e:
        await _run(tmp_path, [agentic.Reply(text="still going",
                                            calls=[_call("run_shell", {"command": "true"})])],
                   max_turns=3, repeat_last=True)
    assert "maximum number of turns" in str(e.value).lower()
    assert "still going" in str(e.value)      # the retry needs to know where it got to


async def test_a_rate_limit_reads_as_capacity_not_as_a_quality_failure(monkeypatch):
    """Misclassifying a throttle sends the task up the escalation ladder instead of
    just waiting for the window to open."""
    class _Resp:
        status_code = 429
        headers = {"retry-after": "1"}
        text = "slow down"

        def json(self):
            return {"error": {"message": "rate limit exceeded"}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(agentic.httpx, "AsyncClient", lambda **k: _Client())
    monkeypatch.setattr(agentic.asyncio, "sleep", _no_sleep)
    with pytest.raises(agentic.AgenticError) as e:
        await agentic._post("https://example.invalid", {}, {}, "OpenAI")
    assert launcher.looks_rate_limited(str(e.value))


async def _no_sleep(_seconds):
    return None


async def test_cost_is_accumulated_and_survives_a_session_that_dies(tmp_path):
    """A run that burns its whole budget and then hits the turn limit must not
    report $0.00, or the project cap never sees the most expensive runs."""
    spend = {"usd": 0.0}
    engine = FakeEngine([agentic.Reply(cost=0.25,
                                       calls=[_call("run_shell", {"command": "true"})])],
                        repeat_last=True)
    with pytest.raises(agentic.AgenticError):
        await agentic.run_session(provider="openai", model="gpt-5-mini", key="k",
                                  system="s", prompt="p", repo_dir=tmp_path,
                                  max_turns=4, engine=engine, spend=spend)
    assert spend["usd"] == pytest.approx(1.0)


def test_token_prices_match_the_longest_model_prefix_and_unknowns_cost_nothing():
    assert agentic.price_of("gpt-5-mini", 1_000_000, 0) == pytest.approx(0.25)
    assert agentic.price_of("gpt-5", 1_000_000, 0) == pytest.approx(1.25)
    assert agentic.price_of("gemini-flash-lite-latest", 0, 1_000_000) == pytest.approx(0.40)
    # A model nobody priced must not be invoiced as free-with-confidence elsewhere;
    # zero is the honest answer and the run is still capped by the agent-run limit.
    assert agentic.price_of("some-model-launched-tomorrow", 10**6, 10**6) == 0.0


async def test_the_loop_reports_what_the_agent_is_doing_as_it_happens(tmp_path):
    events = []
    await _run(tmp_path, [
        agentic.Reply(text="starting", calls=[_call("run_shell", {"command": "true"})]),
        agentic.Reply(text="finished"),
    ], events=events)
    kinds = [k for k, _ in events]
    assert kinds == ["message", "tool_use", "message"]
    assert "run_shell" in events[1][1]


# ---- provider wire formats -------------------------------------------------


async def test_openai_tool_calls_are_parsed_and_answered_in_its_own_shape(monkeypatch):
    """OpenAI rejects a tool result whose call it never saw us receive, so the
    assistant message goes back into the history verbatim."""
    payload = {"choices": [{"message": {
        "role": "assistant", "content": "working",
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "read_file",
                                     "arguments": '{"path": "x.py"}'}},
                       {"id": "call_2", "type": "function",
                        "function": {"name": "read_file", "arguments": "{not json"}}]}}],
        "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}

    async def _fake_post(url, headers, body, label):
        _fake_post.body = body
        return payload

    monkeypatch.setattr(agentic, "_post", _fake_post)
    e = agentic.OpenAIEngine("gpt-5-mini", "key")
    e.start("system", "task")
    reply = await e.turn(agentic.TOOL_SPECS)

    assert reply.text == "working"
    assert reply.cost == pytest.approx(0.25)
    assert [c.name for c in reply.calls] == ["read_file", "read_file"]
    assert reply.calls[0].args == {"path": "x.py"}
    assert reply.calls[1].args is None          # malformed, and reported as such
    assert _fake_post.body["tools"][0]["type"] == "function"
    # gpt-5 rejects max_tokens under its old name
    assert "max_completion_tokens" in _fake_post.body

    e.record_result(reply.calls[0], "file contents")
    assert e.messages[-1] == {"role": "tool", "tool_call_id": "call_1",
                              "name": "read_file", "content": "file contents"}
    assert e.messages[-2]["tool_calls"][0]["id"] == "call_1"


async def test_gemini_gets_proto_style_schemas_and_one_grouped_reply(monkeypatch):
    """Gemini expects the answers to a turn that asked for two calls to arrive
    together; sending them as separate turns is how a session gets rejected."""
    payload = {"candidates": [{"content": {"parts": [
        {"text": "thinking"},
        {"functionCall": {"name": "list_files", "args": {"pattern": "*.py"}}},
        {"functionCall": {"name": "read_file", "args": {"path": "a.py"}}}]}}],
        "usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 1_000_000}}

    bodies = []

    async def _fake_post(url, headers, body, label):
        bodies.append(copy.deepcopy(body))     # the engine keeps mutating its history
        return payload

    monkeypatch.setattr(agentic, "_post", _fake_post)
    e = agentic.GeminiEngine("gemini-flash-latest", "key")
    e.start("system", "task")
    reply = await e.turn(agentic.TOOL_SPECS)

    assert reply.text == "thinking"
    assert [c.name for c in reply.calls] == ["list_files", "read_file"]
    assert reply.cost == pytest.approx(2.50)
    decls = bodies[0]["tools"][0]["functionDeclarations"]
    assert decls[0]["parameters"]["type"] == "OBJECT"
    assert all(p["type"].isupper()
               for p in decls[0]["parameters"]["properties"].values())

    for c in reply.calls:
        e.record_result(c, "result")
    await e.turn(agentic.TOOL_SPECS)
    sent = bodies[1]["contents"]
    assert sent[-1]["role"] == "user"
    assert len(sent[-1]["parts"]) == 2          # one turn, both answers
    assert sent[-2]["role"] == "model"


def test_an_engine_is_only_built_for_a_provider_that_has_one():
    assert isinstance(agentic.engine_for("openai", "gpt-5", "k"), agentic.OpenAIEngine)
    assert isinstance(agentic.engine_for("google", "gemini-flash-latest", "k"),
                      agentic.GeminiEngine)
    with pytest.raises(agentic.AgenticError):
        agentic.engine_for("anthropic", "claude-sonnet-5", "k")


# ---- what the conductor hands the worker -----------------------------------


def test_the_launcher_tells_the_worker_which_engine_to_run(fresh_db):
    pid = make_project(name="byok")
    team.hire(pid, [{"role": "backend", "count": 1, "provider": "google",
                     "model": "gemini-flash-latest"}])
    tid = make_task(pid, role="backend")
    agent = team.claim(db.get_task(tid))
    env = launcher._worker_env(db.get_task(tid), db.get_project(pid), "claude-haiku-4-5")

    assert env["PROVIDER"] == "google"
    assert env["MODEL"] == "gemini-flash-latest"     # the teammate's own choice wins
    assert agent["name"] in env["AGENT_CONTEXT"]


def test_a_non_claude_teammate_is_never_dispatched_on_a_claude_model(fresh_db):
    """Every model-picking rule predates non-Claude workers, so the rate-limit
    fallback and the escalation ladder both hand out Claude models. Asking OpenAI
    for claude-haiku is a 404 and a wasted attempt."""
    pid = make_project(name="byok2")
    team.hire(pid, [{"role": "backend", "count": 1, "provider": "openai"}])
    tid = make_task(pid, role="backend")
    team.claim(db.get_task(tid))
    env = launcher._worker_env(db.get_task(tid), db.get_project(pid), "claude-opus-4-8")

    assert env["PROVIDER"] == "openai"
    assert env["MODEL"].startswith("gpt")


def test_a_project_with_no_team_still_runs_exactly_as_before(fresh_db):
    """Projects created before teammates existed have no agent rows, and the
    Claude path must not notice that any of this happened."""
    pid = make_project(name="old")
    tid = make_task(pid, role="backend")
    env = launcher._worker_env(db.get_task(tid), db.get_project(pid), "claude-haiku-4-5")
    assert env["PROVIDER"] == "anthropic"
    assert env["MODEL"] == "claude-haiku-4-5"


def test_a_user_with_only_an_openai_key_can_still_have_agents_dispatched(fresh_db,
                                                                        make_user):
    """The gap this closes: the dispatch gate used to demand an Anthropic key, so
    an OpenAI-only user could staff a round table and never get a line of code."""
    from app import auth

    uid, _ = make_user("openai-only")
    auth.save_settings(uid, {"openai_api_key": "sk-test-not-real"})
    creds = launcher.owner_credentials({"owner_id": uid})

    assert creds["OPENAI_API_KEY"] == "sk-test-not-real"
    # and the operator's own credentials are still actively blanked, not inherited
    assert creds["ANTHROPIC_API_KEY"] == ""
    assert creds["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert creds["GEMINI_API_KEY"] == ""
