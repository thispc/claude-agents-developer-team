"""Suggest a starting team composition ("recruiting") from a project brief.

One cheap completion (no tools) proposing roles + counts + model tier, so the boss
sees a sensible pre-filled team to tweak. Falls back to a keyword heuristic if the
call fails or returns junk, so recruiting never blocks.

It goes through `providers` rather than the Claude SDK directly: this is one
prompt in, one JSON array out with no tool use, so it works on any provider the
user holds a key for. A Gemini-only user used to fall silently through to the
keyword heuristic and never know a model had been asked at all.
"""

import json
import logging
import re

from . import config, providers

log = logging.getLogger(__name__)

_ALLOWED_MODELS = {"worker", "lead"}

# The cheap tier of each provider — this is a staffing suggestion, not deep work.
# None means "use the platform's configured worker model".
_PLANNER_MODEL = {"anthropic": None, "google": "gemini-flash-latest", "openai": "gpt-5-mini"}


def _pick(settings: dict) -> tuple[str, str] | None:
    """Cheapest capable model from whatever this user actually holds.

    Claude is preferred only because it is what the rest of the platform runs on;
    any of the three can do a one-shot staffing suggestion.
    """
    have = providers.available(settings)
    for provider in ("anthropic", "google", "openai"):
        if provider in have:
            return provider, _PLANNER_MODEL[provider] or config.WORKER_MODEL
    return None


def _known_roles() -> list[str]:
    return [r["name"] for r in config.load_roles()]


def _heuristic(brief: str) -> list[dict]:
    b = brief.lower()
    team: list[dict] = []
    wants_backend = any(k in b for k in
                        ("api", "backend", "server", "database", "endpoint", "auth", "fastapi", "express"))
    wants_frontend = any(k in b for k in
                         ("ui", "frontend", "page", "web app", "html", "react", "dashboard", "interface", "css"))
    if wants_backend or not wants_frontend:
        team.append({"role": "backend", "count": 1, "model": "worker"})
    if wants_frontend:
        team.append({"role": "frontend", "count": 1, "model": "worker"})
    team.append({"role": "tester", "count": 1, "model": "worker"})
    return team


def _trim_role(name: str, limit: int = 32) -> str:
    """Cap a role name without slicing a word in half.

    Roles become branch names and UI labels so they need a bound, but a hard
    slice produced 'space_qualification_and_link_tes' and
    'pointing_acquisition_tracking_en' — which read as bugs, because they are.
    """
    if len(name) <= limit:
        return name
    cut = name[:limit]
    for sep in ("_", "-"):
        if sep in cut:
            return cut.rsplit(sep, 1)[0]
    return cut


def _sanitize(items: list, brief: str) -> list[dict]:
    known = set(_known_roles())
    out: list[dict] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or "role" not in it:
            continue
        role = _trim_role(str(it["role"]).strip().lower().replace(" ", "-"))
        if not role:
            continue
        count = it.get("count", 1)
        count = count if isinstance(count, int) and 1 <= count <= 8 else 1
        model = it.get("model", "worker")
        model = model if model in _ALLOWED_MODELS else "worker"
        out.append({"role": role, "count": count, "model": model,
                    "known": role in known})
    return out or _heuristic(brief)


SYSTEM = (
    "You are an expert staffing planner. Given ANY project brief — software, hardware, "
    "research, design, writing, a rocket blueprint, a business plan, anything — assemble "
    "the ideal team to execute it. Invent whatever specialist roles the domain actually "
    "needs; do NOT default to software roles unless the project is software. For a rocket "
    "you might staff propulsion_engineer, structures_engineer, avionics_engineer; for a "
    "novel, plot_architect, prose_writer, editor; for software, backend, frontend, tester.\n\n"
    "Return ONLY a JSON array of objects with keys: role (lowercase snake_case), count "
    "(1-4; use >1 only when the work has independent parallelizable parts), and model "
    "('worker' = cheap/fast for routine or well-defined work, 'lead' = stronger/pricier for "
    "hard design, deep reasoning, or research). Assign the stronger model to the roles doing "
    "the hardest thinking, the cheap model to routine execution. Include a review/verify "
    "role appropriate to the domain (tester for software, reviewer/editor otherwise). Keep "
    "the team lean and appropriate."
)


async def suggest_team(brief: str, settings: dict | None = None) -> list[dict]:
    chosen = _pick(settings or {})
    if not chosen:
        log.warning("recruiting fell back to the keyword heuristic: "
                    "this user holds no provider credentials")
        return _heuristic(brief)
    provider, model = chosen
    try:
        text = await providers.complete(provider, model, SYSTEM, f"Brief:\n{brief}",
                                        settings or {}, max_tokens=1200)
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            return _sanitize(json.loads(m.group(0)), brief)
        log.warning("recruiting: %s/%s returned no JSON array", provider, model)
    except Exception as e:
        # Recruiting must never block a project start — but a silent fallback is
        # how a broken key looked identical to a working one for weeks.
        log.warning("recruiting via %s/%s failed, using the heuristic: %s",
                    provider, model, e)
    return _heuristic(brief)
