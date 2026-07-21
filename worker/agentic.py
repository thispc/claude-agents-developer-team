"""An agentic loop for the providers that do not ship one.

The Claude path in worker.py is the Agent SDK: it owns the conversation, the
tools and the turn budget. OpenAI and Google sell a model, not an agent — so
"bring your own AI keys" was true for planning, where one completion is enough,
and false for building, where the model has to read the repo, change it and
check its own work. A user with only an OpenAI or Gemini key could staff a round
table and never get a line of code written.

This module is the missing half: a tool-calling conversation rooted in the clone
the worker already prepared, bounded by the same turn budget, ending with the
same final summary the conductor already knows how to read. Everything after the
loop — verification, commit, push, the report — stays worker.py's and is
untouched, which is what keeps the conductor indifferent to which engine ran.

Native function calling on both providers, deliberately. A model asked to emit
shell commands as prose has to be parsed back out, and a parser that is wrong on
one line in fifty is a parser that eventually runs half a command.
"""

import asyncio
import json
import os
import random
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

TIMEOUT = 300
MAX_TOKENS = 8000

# How much of anything the model gets back. Tool output is the main way a context
# window dies: one `npm install` or one 5 MB lockfile read ends the session with
# no work done and the whole budget spent. Truncation is announced in the text so
# the model knows it saw a slice rather than the whole thing.
FILE_LIMIT = 60_000
SHELL_LIMIT = 16_000
GREP_LIMIT = 80
LIST_LIMIT = 300
SHELL_TIMEOUT = 600


class AgenticError(RuntimeError):
    """The session could not continue. The message reaches the task report verbatim,
    so the launcher's retry logic can read it — see RATE_LIMIT_MARKERS and the
    "maximum number of turns" check in conductor/app/launcher.py. Reword with care."""


# --------------------------------------------------------------------------
# Tools
#
# The same six capabilities the Claude path grants (Read/Write/Edit/Bash/Glob/
# Grep), described once in plain JSON Schema and translated per provider.
# `ask_teammate` is deliberately absent: consulting goes through the Claude Agent
# SDK, which a user who brought only an OpenAI key cannot authenticate. A tool
# that always answers "your teammate could not be reached" is worse than no tool,
# because the model spends turns on it.

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a file from the repository. Paths are relative to the "
                       "repository root. Long files come back truncated, and say so.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "e.g. src/api/main.py"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with the exact content given. "
                       "Parent directories are created. Use edit_file to change part "
                       "of a large file instead of rewriting it.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"},
                           "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace an exact snippet in a file. `old` must appear exactly "
                       "once — if it does not, nothing is changed and you are told so.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"},
                           "old": {"type": "string", "description": "exact text to replace"},
                           "new": {"type": "string", "description": "replacement text"}},
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "list_files",
        "description": "List files matching a glob, e.g. '**/*.py' or 'src/*.ts'. "
                       "Skips dependency and build directories.",
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "search",
        "description": "Search file contents with a regular expression and return "
                       "matching lines with their paths and line numbers.",
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string"},
                           "glob": {"type": "string",
                                    "description": "optional file filter, e.g. '**/*.py'"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "run_shell",
        "description": "Run a shell command in the repository root: build, install, "
                       "run the tests, anything. Returns exit code, stdout and stderr. "
                       "Do not run `git push` and do not open a pull request — the "
                       "platform does both for you after you finish.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]

# Directories nobody means to search: other people's code and build output.
_SKIP = {"node_modules", "site-packages", "vendor", "dist", "build", "__pycache__",
         ".git", "venv", ".venv", "env", ".tox"}


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n… truncated, {len(text) - limit} more characters"


def build_tools(repo_dir: Path) -> dict[str, Callable[[dict], str]]:
    """The tool implementations, bound to one clone.

    Paths resolve against the clone, matching the SDK path's `cwd`. There is no
    confinement check beyond that and it would be theatre if there were: the same
    toolset hands the model a shell, so a tool-layer guard on read_file stops
    nothing that `cat` does not already do. The disposable clone is the boundary,
    and it is the same boundary the Claude path has always run behind.
    """

    def _path(args: dict, key: str = "path") -> Path:
        raw = str(args.get(key) or "").strip()
        if not raw:
            raise ValueError(f"{key} is required")
        p = Path(raw)
        return p if p.is_absolute() else repo_dir / p

    def read_file(args: dict) -> str:
        p = _path(args)
        if not p.exists():
            return f"no such file: {args.get('path')}"
        if p.is_dir():
            return f"{args.get('path')} is a directory; use list_files"
        return _truncate(p.read_text(errors="replace"), FILE_LIMIT)

    def write_file(args: dict) -> str:
        p = _path(args)
        content = args.get("content")
        if content is None:
            return "content is required (send an empty string to truncate the file)"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(content))
        return f"wrote {len(str(content))} characters to {args.get('path')}"

    def edit_file(args: dict) -> str:
        p = _path(args)
        old, new = str(args.get("old", "")), str(args.get("new", ""))
        if not p.exists():
            return f"no such file: {args.get('path')}"
        if not old:
            return "old is required; use write_file to create a file from scratch"
        body = p.read_text(errors="replace")
        found = body.count(old)
        if found == 0:
            return ("that exact text is not in the file — read it again and match the "
                    "indentation exactly")
        if found > 1:
            return (f"that text appears {found} times; include more surrounding lines "
                    "so it identifies one place")
        p.write_text(body.replace(old, new, 1))
        return f"edited {args.get('path')}"

    def _walk() -> list[Path]:
        out = []
        for root, dirs, files in os.walk(repo_dir):
            dirs[:] = [d for d in dirs if d not in _SKIP and not d.startswith(".")]
            for f in files:
                out.append(Path(root) / f)
        return out

    def list_files(args: dict) -> str:
        pattern = str(args.get("pattern") or "**/*")
        hits = [str(p.relative_to(repo_dir)) for p in _walk()
                if p.match(pattern) or str(p.relative_to(repo_dir)) == pattern]
        if not hits:
            return f"nothing matches {pattern}"
        hits.sort()
        extra = len(hits) - LIST_LIMIT
        listed = "\n".join(hits[:LIST_LIMIT])
        return listed + (f"\n… and {extra} more" if extra > 0 else "")

    def search(args: dict) -> str:
        try:
            rx = re.compile(str(args.get("pattern") or ""))
        except re.error as e:
            return f"that is not a valid regular expression: {e}"
        glob = str(args.get("glob") or "")
        out: list[str] = []
        for p in _walk():
            rel = str(p.relative_to(repo_dir))
            if glob and not p.match(glob):
                continue
            try:
                text = p.read_text(errors="strict")
            except (OSError, UnicodeDecodeError):
                continue          # binaries and unreadable files are not search results
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    out.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(out) >= GREP_LIMIT:
                        return "\n".join(out) + f"\n… stopped at {GREP_LIMIT} matches"
        return "\n".join(out) if out else "no matches"

    def run_shell(args: dict) -> str:
        cmd = str(args.get("command") or "").strip()
        if not cmd:
            return "command is required"
        try:
            r = subprocess.run(cmd, cwd=repo_dir, shell=True, capture_output=True,
                               text=True, timeout=SHELL_TIMEOUT,
                               env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except subprocess.TimeoutExpired:
            return f"command timed out after {SHELL_TIMEOUT}s: {cmd}"
        body = ((r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")).strip()
        return f"exit code {r.returncode}\n{_truncate(body, SHELL_LIMIT) or '(no output)'}"

    return {"read_file": read_file, "write_file": write_file, "edit_file": edit_file,
            "list_files": list_files, "search": search, "run_shell": run_shell}


# How the loop tells the model what it is and how to stop. The role prompt is
# written for the Claude path's built-in tools, so without this the model is told
# to "edit the file" with no idea what it is allowed to call.
TOOL_BRIEFING = """

## How you work here

You are driving a real repository checkout through tools: read_file, write_file,
edit_file, list_files, search and run_shell. Everything happens in the repository
root; you have a shell, so build it, install it and run its tests for real.

Call tools until the work is genuinely done — do not describe an edit you have
not made, and do not claim a test passed unless you ran it. When you are
finished, reply with your final summary and no tool call at all: that message
ends the session and is what your manager reads. Include what you did, the files
you touched, how to verify it, and anything you could not do.
"""


# --------------------------------------------------------------------------
# Provider engines


@dataclass
class ToolCall:
    id: str
    name: str
    # None when the model sent arguments that were not parseable JSON. That is a
    # normal event, not a crash — the model is told and gets to try again.
    args: dict | None
    raw: str = ""


@dataclass
class Reply:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    cost: float = 0.0


# $ per million tokens (input, output), longest-prefix match. List prices drift,
# and a stale number here is still far better than zero: the project budget check
# reads this, and a worker that always reports $0.00 spends an unbounded amount
# under a cap that never trips.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "o3-mini": (1.10, 4.40),
    "o3": (2.00, 8.00),
    "gemini-flash-lite": (0.10, 0.40),
    "gemini-flash": (0.30, 2.50),
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-pro": (1.25, 10.00),
}


def price_of(model: str, prompt_tokens: int, output_tokens: int) -> float:
    rate = None
    for prefix in sorted(PRICES, key=len, reverse=True):
        if model.startswith(prefix):
            rate = PRICES[prefix]
            break
    if not rate:
        return 0.0
    return prompt_tokens * rate[0] / 1e6 + output_tokens * rate[1] / 1e6


RETRY_STATUS = {408, 500, 502, 503, 529}
RETRIES = 3


async def _post(url: str, headers: dict, body: dict, label: str) -> dict:
    """One provider call, with the throttle handling the round table already has.

    A 429 that survives the retries is raised with the words "rate limit" in it on
    purpose: that is what the launcher matches to tell a capacity death from a
    quality failure, and a mislabelled capacity death sends the task up the
    escalation ladder instead of just waiting.
    """
    last = ""
    for attempt in range(RETRIES):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.post(url, headers=headers, json=body)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt == RETRIES - 1:
                raise AgenticError(f"{label} was unreachable: {e}") from e
            await asyncio.sleep(1.5 * 2 ** attempt + random.random())
            continue
        if r.status_code == 200:
            return r.json()
        try:
            last = str(r.json().get("error", {}).get("message", ""))[:300] or r.text[:300]
        except Exception:
            last = r.text[:300]
        if r.status_code == 429:
            if attempt == RETRIES - 1:
                raise AgenticError(f"{label} rate limit (429): {last}")
            wait = _retry_after(r) or (2.0 * 2 ** attempt)
            await asyncio.sleep(min(wait, 30) + random.random())
            continue
        if r.status_code not in RETRY_STATUS or attempt == RETRIES - 1:
            raise AgenticError(f"{label} returned {r.status_code}: {last}")
        await asyncio.sleep(1.5 * 2 ** attempt + random.random())
    raise AgenticError(f"{label} failed after {RETRIES} attempts: {last}")


def _retry_after(resp: httpx.Response) -> float:
    """Google buries the delay in error.details[].retryDelay; OpenAI uses the header."""
    hdr = resp.headers.get("retry-after", "")
    if hdr.strip().replace(".", "", 1).isdigit():
        return float(hdr)
    try:
        for d in resp.json().get("error", {}).get("details", []):
            raw = str(d.get("retryDelay", ""))
            if raw.endswith("s") and raw[:-1].replace(".", "", 1).isdigit():
                return float(raw[:-1])
    except Exception:
        pass
    return 0.0


def _parse_args(raw: str) -> dict | None:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class OpenAIEngine:
    """Chat Completions with native tools. The assistant message is appended
    verbatim, tool_calls and all — OpenAI rejects a tool result whose call it
    never saw us receive."""

    label = "OpenAI"

    def __init__(self, model: str, key: str) -> None:
        self.model, self.key = model, key
        self.messages: list[dict] = []

    def start(self, system: str, prompt: str) -> None:
        self.messages = [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}]

    async def turn(self, tools: list[dict]) -> Reply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            "tools": [{"type": "function", "function": t} for t in tools],
            "tool_choice": "auto",
        }
        # The reasoning models reject temperature and rename the token budget.
        if self.model.startswith(("o1", "o3", "o4", "gpt-5")):
            body["max_completion_tokens"] = MAX_TOKENS
        else:
            body["max_tokens"] = MAX_TOKENS
        data = await _post("https://api.openai.com/v1/chat/completions",
                           {"Authorization": f"Bearer {self.key}"}, body, self.label)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise AgenticError(f"OpenAI returned no message: {str(data)[:300]}") from e
        self.messages.append(msg)
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""),
                                  args=_parse_args(raw), raw=raw))
        usage = data.get("usage") or {}
        return Reply(text=msg.get("content") or "", calls=calls,
                     cost=price_of(self.model, usage.get("prompt_tokens", 0),
                                   usage.get("completion_tokens", 0)))

    def record_result(self, call: ToolCall, output: str) -> None:
        self.messages.append({"role": "tool", "tool_call_id": call.id,
                              "name": call.name, "content": output})


def _gemini_schema(schema: dict) -> dict:
    """Gemini takes proto3 JSON, where a type is the enum NAME: OBJECT, not object."""
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k == "type":
            out[k] = str(v).upper()
        elif k == "properties" and isinstance(v, dict):
            out[k] = {n: _gemini_schema(s) for n, s in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = _gemini_schema(v)
        else:
            out[k] = v
    return out


class GeminiEngine:
    """generateContent with functionDeclarations.

    Function results are buffered and flushed as one user turn: Gemini expects the
    responses to a model turn that requested three calls to arrive together, and
    sending them as three separate turns is how a conversation ends up rejected
    mid-session for a mismatched part count.
    """

    label = "Gemini"

    def __init__(self, model: str, key: str) -> None:
        self.model, self.key = model, key
        self.system = ""
        self.contents: list[dict] = []
        self._pending: list[dict] = []

    def start(self, system: str, prompt: str) -> None:
        self.system = system
        self.contents = [{"role": "user", "parts": [{"text": prompt}]}]

    async def turn(self, tools: list[dict]) -> Reply:
        if self._pending:
            self.contents.append({"role": "user", "parts": self._pending})
            self._pending = []
        body = {
            "systemInstruction": {"parts": [{"text": self.system}]},
            "contents": self.contents,
            "tools": [{"functionDeclarations": [
                {"name": t["name"], "description": t["description"],
                 "parameters": _gemini_schema(t["parameters"])} for t in tools]}],
            "generationConfig": {"maxOutputTokens": MAX_TOKENS},
        }
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{self.model}:generateContent")
        data = await _post(url, {"x-goog-api-key": self.key}, body, self.label)
        try:
            content = data["candidates"][0]["content"]
        except (KeyError, IndexError) as e:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            raise AgenticError(
                f"Gemini returned no content{f' ({blocked})' if blocked else ''}") from e
        self.contents.append({"role": "model", "parts": content.get("parts") or []})
        text, calls = "", []
        for i, part in enumerate(content.get("parts") or []):
            if "text" in part:
                text += part["text"]
            fc = part.get("functionCall")
            if fc:
                args = fc.get("args")
                calls.append(ToolCall(id=f"{fc.get('name', '')}-{i}",
                                      name=fc.get("name", ""),
                                      args=args if isinstance(args, dict) else None,
                                      raw=json.dumps(args)[:400]))
        usage = data.get("usageMetadata") or {}
        return Reply(text=text, calls=calls,
                     cost=price_of(self.model, usage.get("promptTokenCount", 0),
                                   usage.get("candidatesTokenCount", 0)))

    def record_result(self, call: ToolCall, output: str) -> None:
        self._pending.append({"functionResponse": {"name": call.name,
                                                   "response": {"output": output}}})


def engine_for(provider: str, model: str, key: str):
    if provider == "openai":
        return OpenAIEngine(model, key)
    if provider in ("google", "gemini"):
        return GeminiEngine(model, key)
    raise AgenticError(f"no agentic engine for provider {provider!r}")


# --------------------------------------------------------------------------
# The loop


def dispatch(tools: dict[str, Callable[[dict], str]], call: ToolCall) -> str:
    """Run one tool call and always come back with something the model can read.

    Nothing in here raises. A tool that blows up is information — "the path does
    not exist", "your regex is malformed" — and handing that back costs one turn,
    whereas letting it propagate kills a session that may already have hours of
    work in the clone.
    """
    if call.name not in tools:
        return (f"there is no tool called {call.name!r}. Available: "
                f"{', '.join(sorted(tools))}")
    if call.args is None:
        return ("your arguments were not valid JSON, so nothing ran. Send them again "
                "as a JSON object.")
    try:
        return str(tools[call.name](call.args))
    except Exception as e:
        return f"{call.name} failed: {type(e).__name__}: {e}"


async def run_session(provider: str, model: str, key: str, system: str, prompt: str,
                      repo_dir: Path, max_turns: int,
                      emit: Callable[[str, str], None] | None = None,
                      engine=None, spend: dict | None = None) -> tuple[str, float]:
    """Drive one non-Anthropic model through the task. Returns (final summary, cost).

    The last assistant message with no tool call is the summary — the same contract
    the Claude path fulfils, because the conductor reads a report and does not know
    or care which engine produced it.

    `spend` is a running total the caller can still read after an exception. A
    session that dies on the turn limit spent every dollar of it, and reporting
    $0.00 for it would hide the most expensive runs from the project budget.
    """
    say = emit or (lambda kind, payload: None)
    engine = engine or engine_for(provider, model, key)
    engine.start(system, prompt)
    tools = build_tools(repo_dir)

    last_text, cost = "", 0.0
    for _ in range(max_turns):
        reply = await engine.turn(TOOL_SPECS)
        cost += reply.cost
        if spend is not None:
            spend["usd"] = cost
        if reply.text.strip():
            last_text = reply.text
            say("message", reply.text)
        if not reply.calls:
            return last_text, cost
        for call in reply.calls:
            say("tool_use", f"{call.name}: {(json.dumps(call.args) if call.args is not None else call.raw)[:300]}")
            engine.record_result(call, dispatch(tools, call))

    # Phrased for the launcher: it greps reports for "maximum number of turns" to
    # decide a retry deserves a bigger budget rather than a different model.
    raise AgenticError(
        f"the agent reached the maximum number of turns ({max_turns}) without "
        f"finishing. Last thing it said:\n{last_text[:1000]}")
