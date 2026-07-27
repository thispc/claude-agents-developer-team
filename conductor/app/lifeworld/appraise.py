"""Appraisers — the thing that turns a signal into a consequence packet.

Two live here, sharing a signature `(human, signal, ctx) -> Packet`:

- `deterministic` (Tier 0) — a free rule table gated by the agent's own traits, so the
  same event lands differently on a fragile agent than a composed one. This is the floor:
  the whole world runs offline on it, and it is what a habit (Tier 1) is compiled *from*.
- `model` (Tier 2) — the one bounded spend, used only for novel or high-stakes signals
  the attention gate let through and no habit covered. It proposes a packet; the clamps
  still own the outcome.

Keeping both behind one signature is what lets the scan loop stay a single clean method
that doesn't care which tier answered.
"""

from __future__ import annotations

import json
from typing import Any

from .types import Packet, Signal


def deterministic(human, signal: Signal, ctx: dict) -> Packet:
    """Tier 0 — free, trait-gated rules. The seed every habit grows from."""
    p = human.psyche
    composure = p.traits["composure"]
    willpower = p.traits["willpower"]
    empathy = p.traits["empathy"]
    kind = signal.kind
    i = signal.intensity
    out = Packet(understood=f"{kind} (i={i:.2f})", tier=0, memory=signal.text() or kind)

    if kind == "scold":
        out.mood = {"stress": +i * (1 - composure),
                    "confidence": -i * (1 - willpower),
                    "hope": -0.4 * i * (1 - composure)}
        out.action = {"kind": "concede", "text": "understood."}
        if signal.from_id:
            out.social = {str(signal.from_id): {"trust": -0.05 * i}}
    elif kind == "praise":
        out.mood = {"confidence": +i * (0.5 + 0.5 * empathy), "hope": +0.5 * i, "stress": -0.4 * i}
        out.action = {"kind": "thank", "text": "thank you."}
        if signal.from_id:
            out.social = {str(signal.from_id): {"warmth": +0.06 * i, "trust": +0.03 * i}}
    elif kind in ("rest", "sit"):
        comfort = float(signal.payload.get("comfort", 0.6))
        out.mood = {"stress": -0.4 * comfort, "focus": +0.2 * comfort}
        out.vitals = {"energy": +0.3 * comfort}
        out.drives = {"energy": +0.3 * comfort}
    elif kind == "win":
        out.mood = {"confidence": +i, "hope": +0.6 * i, "stress": -0.3 * i}
        out.drives = {"esteem": +0.2 * i}
        out.action = {"kind": "exult", "text": "yes!"}
    elif kind == "lose":
        addict = 1 - willpower
        out.mood = {"confidence": -i, "stress": +0.7 * i,
                    "hope": +0.2 * i * addict - 0.3 * i * (1 - addict)}
        out.action = {"kind": "regroup", "text": "again."}
    elif kind == "greet":
        out.mood = {"confidence": +0.05, "focus": +0.05}
        out.drives = {"social": +0.2}
        if signal.from_id:
            out.social = {str(signal.from_id): {"warmth": +0.04, "trust": +0.02}}
    elif kind in ("deal", "reveal", "see"):
        out.understood = f"took in {kind}: {signal.text()[:60]}"
        out.mood = {"focus": +0.1}
    else:
        out.mood = {"focus": +0.03}
    return out


async def model(human, signal: Signal, ctx: dict, *, settings: dict,
                complete, model_name: str, max_tokens: int, rules: str = "") -> Packet:
    """Tier 2 — one bounded call, over only what this agent may see. `complete` is the
    provider function injected by the world, so this module never imports the platform.
    `rules` are the scene's standing rules, obeyed every run."""
    sys = ("You are a character reacting to one event. Report its effect on your inner "
           "state as STRICT JSON: {understood, mood, vitals, drives, memory, action:{kind,text}, "
           "social:{'<id>':{trust,warmth}}}. mood keys: confidence,stress,hope,focus. "
           "Deltas are small suggestions in [-1,1].")
    if rules and rules.strip():
        sys += " SCENE RULES you must obey: " + rules.strip()[:600]
    prompt = (f"YOU: {json.dumps(_self_view(human))}\nEVENT: {json.dumps(_signal_view(signal))}\n"
              f"WHAT YOU RECALL: {human.memory.recall(signal.domain)[:600]}\n\nReturn only the JSON.")
    try:
        raw = await complete("anthropic", model_name, sys, prompt, settings, max_tokens=max_tokens)
    except Exception as e:                      # the floor: fall back to free Tier 0
        p = deterministic(human, signal, ctx)
        p.understood = f"(model unavailable: {str(e)[:60]}) " + p.understood
        return p
    d = _parse(raw)
    # Trust boundary: the model sometimes returns mood/vitals/drives/social/action as a LIST or
    # scalar instead of an object. The appliers call .items()/dict access, so coerce non-dicts to {}
    # (a live agent chat crashed here — 'list' object has no attribute 'items').
    for k in ("mood", "vitals", "drives", "social", "action"):
        if k in d and not isinstance(d[k], dict):
            d[k] = {}
    p = Packet.from_dict(d)
    p.tier, p.spent = 2, 1
    if not p.memory:
        p.memory = p.understood or signal.text()
    return p


def _self_view(human) -> dict:
    return {"name": human.name, "traits": human.psyche.traits, "mood": human.psyche.mood,
            "wants": human.drives.dominant_goal()[0]}


def _signal_view(signal: Signal) -> dict:
    return {"kind": signal.kind, "text": signal.text(), "from": signal.from_id,
            "intensity": round(signal.intensity, 2)}


def _parse(raw: str) -> dict:
    text = (raw or "").strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    try:
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {}
