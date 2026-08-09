"""Authoring — the LLM fills in an agent's or artifact's internal code from a brief.

The operator does not tune an equalizer. They say who a person is ("a confident young
lawyer, sharp but insecure") or what a thing is ("a worn deck of cards"), and a model
authors the internals: the trait dials, a starting narrative, a seed of competencies, the
senses that matter — or, for an artifact, its kind and its public/secret shape. This is
the one bounded spend of creation; when no credentials are present (or the call fails), a
deterministic author reads the brief for keywords, so creation still works offline and
free. Either way the result is validated and clamped before it becomes an entity.
"""

from __future__ import annotations

import json
from typing import Any

from .psyche import TRAITS

# keyword -> {trait: nudge in 0..100 space}, for the free fallback and as a prior.
_WORDS = {
    "confident": {"willpower": 80, "composure": 75}, "bold": {"willpower": 80, "risk_appetite": 70},
    "shy": {"sociability": 25, "composure": 40}, "timid": {"sociability": 25, "willpower": 30},
    "curious": {"curiosity": 85}, "inventive": {"curiosity": 80, "conscientiousness": 40},
    "warm": {"empathy": 80, "sociability": 75}, "kind": {"empathy": 80},
    "reckless": {"risk_appetite": 85, "composure": 30}, "gambler": {"risk_appetite": 85},
    "careful": {"conscientiousness": 80, "composure": 70}, "meticulous": {"conscientiousness": 85},
    "disciplined": {"conscientiousness": 80, "willpower": 75}, "anxious": {"composure": 25},
    "insecure": {"composure": 30, "willpower": 40}, "charming": {"sociability": 85, "empathy": 65},
    "ruthless": {"empathy": 20, "risk_appetite": 70}, "calm": {"composure": 85},
}
_ROLE_SKILL = {
    "lawyer": "law", "attorney": "law", "engineer": "eng.backend", "developer": "eng.backend",
    "designer": "design", "teacher": "teaching", "professor": "teaching", "student": "study",
    "trader": "sales", "salesman": "sales", "doctor": "medicine", "artist": "art",
    "chef": "cooking", "writer": "writing", "musician": "music",
}

ARTIFACT_KINDS = ("deck", "card", "prop")


# --- humans ----------------------------------------------------------------

def _fallback_human(name: str, brief: str) -> dict:
    b = (brief or "").lower()
    dials = {t: 50 for t in TRAITS}
    for word, nudges in _WORDS.items():
        if word in b:
            dials.update(nudges)
    skills = [{"path": path, "xp": 3.0} for word, path in _ROLE_SKILL.items() if word in b][:2]
    return {"dials": dials, "narrative": (brief or f"{name}, newly arrived.").strip()[:280],
            "skills": skills, "senses": ["hearing", "interoception"]}


async def author_human(name: str, brief: str, *, complete=None, settings: dict | None = None,
                       model: str = "claude-haiku-4-5", max_tokens: int = 400) -> dict:
    if not complete:
        return _fallback_human(name, brief)
    sys = ("You author a synthetic person's inner code from a brief. Return STRICT JSON: "
           f'{{"dials":{{trait:0-100}},"narrative":"one line","skills":[{{"path":"eng.backend","xp":3}}],'
           f'"senses":["hearing","interoception","sight"]}}. Traits: {list(TRAITS)}. '
           "Pick dials that embody the brief; give 1-3 starting competencies as dotted paths.")
    try:
        raw = await complete("anthropic", model, sys, f"NAME: {name}\nBRIEF: {brief}",
                             settings or {}, max_tokens=max_tokens)
        data = _parse(raw)
        out = _fallback_human(name, brief)               # sane defaults, then overlay
        if isinstance(data.get("dials"), dict):
            out["dials"].update({k: v for k, v in data["dials"].items() if k in TRAITS})
        for k in ("narrative", "skills", "senses"):
            if data.get(k):
                out[k] = data[k]
        return out
    except Exception:
        return _fallback_human(name, brief)


# --- artifacts -------------------------------------------------------------

def _fallback_artifact(name: str, brief: str) -> dict:
    b = (brief or "").lower()
    if "deck" in b or "cards" in b:
        return {"kind": "deck", "public": {}, "secret_schema": ["rank", "suit"], "dormant": False}
    secret = any(w in b for w in ("secret", "locked", "sealed", "hidden", "diary", "letter"))
    return {"kind": "prop", "public": {"desc": (brief or name)[:80]},
            "secret_schema": (["contents"] if secret else []), "dormant": not secret}


async def author_artifact(name: str, brief: str, *, complete=None, settings: dict | None = None,
                          model: str = "claude-haiku-4-5", max_tokens: int = 300) -> dict:
    if not complete:
        return _fallback_artifact(name, brief)
    sys = ('You author an object from a brief. Return STRICT JSON: '
           '{"kind":"deck|card|prop","public":{...visible fields...},'
           '"secret_schema":["fields that are sealed"],"dormant":true|false}. '
           'A deck of cards is kind "deck". Something with a hidden value has secret_schema.')
    try:
        data = _parse(await complete("anthropic", model, sys, f"NAME: {name}\nBRIEF: {brief}",
                                     settings or {}, max_tokens=max_tokens))
        kind = data.get("kind") if data.get("kind") in ARTIFACT_KINDS else None
        if not kind:
            return _fallback_artifact(name, brief)
        return {"kind": kind, "public": data.get("public", {}) or {},
                "secret_schema": data.get("secret_schema", []) or [],
                "dormant": bool(data.get("dormant", True))}
    except Exception:
        return _fallback_artifact(name, brief)


def _parse(raw: str) -> dict:
    text = (raw or "").strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    try:
        d = json.loads(text[text.index("{"):text.rindex("}") + 1])
        return d if isinstance(d, dict) else {}
    except (ValueError, json.JSONDecodeError):
        return {}
