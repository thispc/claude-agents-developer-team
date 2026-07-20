"""The round table: a circle of diverse agents deliberating into a blueprint.

The structure here is deliberate and evidence-led; see docs/ROUNDTABLE_DESIGN.md
for the citations. In short:

  Round 1  PROPOSE   independent, nobody sees anybody      (kills production
                     blocking and first-speaker anchoring)
  Round 2  CRITIQUE  everyone must name concrete flaws     (dialectical inquiry
                     beats consensus on decision quality)
  Round 3  REVISE    each seat updates its own proposal
  Centre   SYNTHESISE moderator weighs arguments, not votes (unanimity is a
                     conformity signal, not proof)

Every seat speaks exactly once per round and the starting seat rotates, because
equal turn-taking is one of the few measured predictors of group intelligence.
"""

import asyncio
import json
import time
from typing import Any

from . import bus, db, providers

# 3–6 seats: groups of 3–6 outperform 7+, and deliberation degrades past ~7.
MIN_SEATS = 3
MAX_SEATS = 8          # hard cap; the UI warns above SOFT_MAX
SOFT_MAX_SEATS = 6

PHASES = ["propose", "critique", "revise", "synthesis"]


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

def _seat_system(seat: dict, brief: str, skeptic: bool) -> str:
    persona = (seat.get("persona") or "").strip()
    base = (
        f"You are '{seat['name']}', one voice at a planning round table. The group "
        f"must turn a rough idea into a buildable blueprint.\n\n"
        f"THE IDEA:\n{brief}\n\n"
    )
    if persona:
        base += f"YOUR PERSPECTIVE:\n{persona}\n\n"
    base += (
        "House rules:\n"
        "- Be concrete. Name technologies, structures, sequences — not adjectives.\n"
        "- Short. Under 250 words. Padding is not thinking.\n"
        "- Disagreeing well is more useful than agreeing. Never agree merely to "
        "be agreeable; if you change your mind, say which argument moved you.\n"
    )
    if skeptic:
        base += (
            "- You additionally hold the SKEPTIC brief: it is your job to find what "
            "the others are glossing over. Attack the plan, never the seat.\n")
    return base


def _propose_prompt(brief: str) -> str:
    return (
        "Write your own proposal for how to build this. Nobody else's view is "
        "available to you yet — that is deliberate, so think independently.\n\n"
        "Cover:\n"
        "1. What you think is ACTUALLY being asked for (restate it sharply).\n"
        "2. Your approach, concretely.\n"
        "3. The one assumption you are least sure about.\n"
    )


def _critique_prompt(others: list[dict]) -> str:
    joined = "\n\n".join(f"--- {o['name']} proposed ---\n{o['text']}" for o in others)
    return (
        f"Here is what the others proposed.\n\n{joined}\n\n"
        "Now critique. You MUST:\n"
        "1. Name the single weakest assumption on the table and say why it breaks.\n"
        "2. Say which proposal (or which parts) you would build on, and why.\n"
        "3. Name one thing everyone — including you — has ignored.\n\n"
        "Do not summarise the others. Argue."
    )


def _revise_prompt(own: str, critiques: list[dict]) -> str:
    joined = "\n\n".join(f"--- {c['name']} argued ---\n{c['text']}" for c in critiques)
    return (
        f"Your original proposal was:\n{own}\n\n"
        f"The table then argued:\n\n{joined}\n\n"
        "Give your final position. State plainly what you changed and which "
        "argument changed it, and what you still disagree with and why. Holding "
        "your ground with a reason is a valid answer."
    )


SYNTH_SYSTEM = (
    "You are the moderator at the centre of a planning round table. You did not "
    "propose anything; you decide.\n\n"
    "How to weigh what you heard:\n"
    "- Weigh ARGUMENTS, not head-counts. Models tend to converge socially rather "
    "than on the merits, so unanimity is weak evidence — treat it as a warning "
    "sign that nobody looked hard, not as proof.\n"
    "- A single well-argued dissent can outweigh three agreements.\n"
    "- Preserve the strongest surviving objection in your output. A plan that "
    "records what its best critic said is more useful than one that hides it.\n"
    "- Choose. Do not hedge into a plan that is every proposal stapled together."
)


def _synth_prompt(brief: str, transcript: str) -> str:
    return (
        f"THE ORIGINAL IDEA:\n{brief}\n\n"
        f"THE FULL DELIBERATION:\n{transcript}\n\n"
        "Write the blueprint. Return ONLY valid JSON, no prose around it:\n"
        "{\n"
        '  "restated_problem": "what is actually being built, sharply stated",\n'
        '  "approach": "the chosen approach, concrete, 2-4 sentences",\n'
        '  "why": "why this over the alternatives",\n'
        '  "alternatives_rejected": [{"option": "...", "why_not": "..."}],\n'
        '  "risks": [{"risk": "...", "mitigation": "..."}],\n'
        '  "open_questions": ["things the boss should decide"],\n'
        '  "strongest_objection": "the best surviving criticism, and whether it was answered",\n'
        '  "milestones": ["ordered, buildable steps"],\n'
        '  "team": [{"role": "snake_case_role", "count": 1, "why": "..."}]\n'
        "}\n"
        "Roles should fit the domain — do not default to backend/frontend/tester "
        "unless this is genuinely that kind of project."
    )


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------

def _emit(table_id: int, project_id: int, kind: str, payload: Any) -> None:
    bus.emit(project_id or 0, None, "roundtable", kind,
             {"table_id": table_id, **(payload if isinstance(payload, dict) else {"v": payload})})


async def _seat_turn(seat: dict, system: str, prompt: str, settings: dict,
                     table_id: int, project_id: int, phase: str, rnd: int) -> dict:
    """One seat speaking once. A failing seat is silenced, not fatal to the table."""
    started = time.time()
    try:
        text = await providers.complete(
            seat["provider"], seat["model"], system, prompt, settings)
        ok = True
    except providers.ProviderError as e:
        text = f"({seat['name']} could not speak: {e})"
        ok = False
    db.add_turn(table_id, seat["id"], phase, rnd, text, ok)
    _emit(table_id, project_id, "seat_spoke",
          {"seat_id": seat["id"], "seat": seat["name"], "phase": phase,
           "ok": ok, "text": text[:4000], "seconds": round(time.time() - started, 1)})
    return {"seat_id": seat["id"], "name": seat["name"], "text": text, "ok": ok}


async def _phase(seats: list[dict], build_prompt, settings: dict, table_id: int,
                 project_id: int, phase: str, rnd: int, brief: str,
                 skeptic_id: int | None) -> list[dict]:
    """Run one phase: every seat speaks exactly once, concurrently.

    Concurrency is safe *because* each seat's prompt is fixed before the phase
    starts — no seat can see another's turn from the same phase, which is what
    keeps the independence guarantee (and removes first-speaker advantage).
    """
    _emit(table_id, project_id, "phase_started", {"phase": phase, "round": rnd})
    jobs = [
        _seat_turn(s, _seat_system(s, brief, s["id"] == skeptic_id),
                   build_prompt(s), settings, table_id, project_id, phase, rnd)
        for s in seats
    ]
    return list(await asyncio.gather(*jobs))


def _rotate(seats: list[dict], rnd: int) -> list[dict]:
    """Rotate who is listed first. Order still leaks into how text is presented,
    so it is rotated rather than fixed."""
    if not seats:
        return seats
    k = rnd % len(seats)
    return seats[k:] + seats[:k]


async def run_table(table_id: int) -> dict:
    """Run the full deliberation. Returns the blueprint dict."""
    table = db.get_table(table_id)
    if not table:
        raise ValueError("no such round table")
    seats = db.list_seats(table_id)
    if len(seats) < MIN_SEATS:
        raise ValueError(f"a round table needs at least {MIN_SEATS} seats")

    from . import auth
    owner = auth.get_user(table["owner_id"])
    settings = auth.get_settings(owner) if owner else {}
    brief = table["brief"]
    pid = table["project_id"] or 0

    # The skeptic brief goes to one seat — assigned opposition beats hoping.
    skeptic_id = seats[-1]["id"]
    db.update_table(table_id, status="running", started_at=time.time())
    _emit(table_id, pid, "table_started",
          {"seats": [{"id": s["id"], "name": s["name"], "provider": s["provider"],
                      "model": s["model"], "skeptic": s["id"] == skeptic_id}
                     for s in seats]})

    # --- round 1: independent proposals ---------------------------------
    proposals = await _phase(seats, lambda s: _propose_prompt(brief), settings,
                             table_id, pid, "propose", 1, brief, skeptic_id)
    live = [p for p in proposals if p["ok"]]
    if not live:
        db.update_table(table_id, status="failed")
        raise ValueError("no seat could speak — check the API keys for these providers")

    # --- round 2: structured dissent ------------------------------------
    def critique_for(seat):
        others = [p for p in _rotate(live, 2) if p["seat_id"] != seat["id"]]
        return _critique_prompt(others)

    critiques = await _phase(seats, critique_for, settings, table_id, pid,
                             "critique", 2, brief, skeptic_id)
    live_crit = [c for c in critiques if c["ok"]]

    # --- round 3: revise in light of the argument -----------------------
    own = {p["seat_id"]: p["text"] for p in proposals}

    def revise_for(seat):
        others = [c for c in _rotate(live_crit, 3) if c["seat_id"] != seat["id"]]
        return _revise_prompt(own.get(seat["id"], "(no proposal)"), others)

    finals = await _phase(seats, revise_for, settings, table_id, pid,
                          "revise", 3, brief, skeptic_id)

    # --- centre: synthesis ----------------------------------------------
    _emit(table_id, pid, "phase_started", {"phase": "synthesis", "round": 4})
    transcript = _transcript(proposals, critiques, finals)
    mod_provider = table["mod_provider"] or seats[0]["provider"]
    mod_model = table["mod_model"] or seats[0]["model"]
    try:
        raw = await providers.complete(mod_provider, mod_model, SYNTH_SYSTEM,
                                       _synth_prompt(brief, transcript), settings,
                                       max_tokens=3000)
    except providers.ProviderError as e:
        db.update_table(table_id, status="failed")
        _emit(table_id, pid, "table_failed", {"error": str(e)})
        raise ValueError(f"the moderator could not synthesise: {e}")

    blueprint = _parse_blueprint(raw)
    db.add_turn(table_id, None, "synthesis", 4, raw, True)
    db.update_table(table_id, status="done", blueprint=json.dumps(blueprint),
                    finished_at=time.time())
    _emit(table_id, pid, "blueprint_ready", {"blueprint": blueprint})
    return blueprint


def _transcript(proposals, critiques, finals) -> str:
    out = ["=== ROUND 1: independent proposals ==="]
    for p in proposals:
        out.append(f"[{p['name']}]\n{p['text']}")
    out.append("\n=== ROUND 2: critique ===")
    for c in critiques:
        out.append(f"[{c['name']}]\n{c['text']}")
    out.append("\n=== ROUND 3: final positions ===")
    for f in finals:
        out.append(f"[{f['name']}]\n{f['text']}")
    return "\n\n".join(out)


def _parse_blueprint(raw: str) -> dict:
    """Models wrap JSON in prose or fences however much you ask them not to."""
    text = raw.strip()
    if "```" in text:
        chunks = text.split("```")
        for ch in chunks:
            ch = ch.strip()
            if ch.startswith("json"):
                ch = ch[4:].strip()
            if ch.startswith("{"):
                text = ch
                break
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except Exception:
        # Never lose the work: keep the prose so the boss can still read it.
        return {"restated_problem": "", "approach": raw[:4000], "why": "",
                "alternatives_rejected": [], "risks": [], "open_questions": [],
                "strongest_objection": "", "milestones": [], "team": [],
                "unparsed": True}
    data.setdefault("team", [])
    data.setdefault("milestones", [])
    data.setdefault("risks", [])
    data.setdefault("alternatives_rejected", [])
    data.setdefault("open_questions", [])
    return data


def homogeneity_warning(seats: list[dict]) -> str:
    """The table is only worth running if the seats actually differ."""
    provs = {s["provider"] for s in seats}
    models = {(s["provider"], s["model"]) for s in seats}
    if len(provs) == 1 and len(models) == 1:
        return ("Every seat is the same provider AND the same model. Debate between "
                "identical models is the case the research found does NOT beat just "
                "asking one model once — vary the provider or at least the model.")
    if len(provs) == 1:
        return ("All seats are on one provider. Mixing providers is the thing that "
                "measurably improves this kind of deliberation.")
    return ""
