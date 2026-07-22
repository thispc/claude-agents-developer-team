"""An agent's episodic memory, folded down into a bounded long-term memory.

This is the ONE part of the Studio that spends tokens, and it spends them as
rarely and as cheaply as the design allows. Three properties matter, in order:

- **Size-triggered, never time-triggered.** An agent is due for consolidation
  when it has accumulated enough unconsolidated episodes — not after an interval.
  An idle agent accumulates nothing and is never due, which is the whole reason
  background memory costs proportional to work rather than to the clock.

- **Additive-then-swap.** The compressed summary is written first, and only then
  are the raw episodes marked consolidated. A model call that fails mid-fold
  therefore loses nothing: the episodes are still there, still unconsolidated,
  and the next tick tries again. Compression makes memory *better*, never
  *possible* — the deterministic floor below is what makes it possible.

- **A deterministic floor.** When the model is unavailable or the day's token
  budget is spent, `_truncate_fold` folds the same episodes down with no
  inference at all — append the gists, cap to the char limit. This is exactly
  the naive `team.release` behaviour, kept deliberately as the floor so memory
  always advances even when nothing may bill.

Retrieval is free and O(1): the bounded blob is inlined into the worker's prompt
through the existing `team.system_addendum` seam. There is nothing to search.
"""

import json
from typing import Any

from . import bus, db, providers, tuning

SECTIONS = ("summary", "skills", "decisions", "relationships")

SYSTEM = (
    "You maintain one teammate's long-term memory. Fold the new events into the "
    "existing memory and return the whole thing, updated.\n\n"
    "Rules:\n"
    "- This is what the teammate is handed the next time they start work — a "
    "  reminder, not a transcript. Keep it tight.\n"
    "- Preserve concrete decisions, skills demonstrated, and who-did-what. Drop "
    "  restatements, pleasantries, and anything a competent teammate would assume.\n"
    "- Weight recent and repeated things over one-offs.\n"
    "- Return ONLY valid JSON with these keys, each a short paragraph or list:\n"
    '  {"summary": "...", "skills": "...", "decisions": "...", "relationships": "..."}'
)


def is_due(home_id: int) -> bool:
    """Whether this agent has piled up enough to be worth a fold. Free — it counts
    rows, it does not think."""
    return db.unconsolidated_count(home_id) >= int(tuning.get("home_episode_threshold"))


def current_blob(home_id: int) -> str:
    """The long-term memory as the worker sees it: sections joined, hard-capped.

    Capped here rather than only at write time because the cap is a knob and a
    worker started after the knob was lowered must still be handed a bounded
    memory, not whatever was stored under the old ceiling.
    """
    mem = db.get_memory(home_id)
    parts = [f"{s}: {mem[s].strip()}" for s in SECTIONS if mem.get(s, "").strip()]
    blob = "\n".join(parts)
    cap = int(tuning.get("home_memory_char_cap"))
    return blob[-cap:] if len(blob) > cap else blob


def _truncate_fold(home_id: int, episodes: list[dict]) -> None:
    """The deterministic floor: fold episodes into the summary with no model.

    Newest last, oldest dropped when over the cap — the same shape `team.release`
    uses for a project note. This runs when the model cannot or must not, so
    memory never stalls on a provider outage or an exhausted budget.
    """
    mem = db.get_memory(home_id)
    lines = [ln for ln in (mem.get("summary", "") or "").splitlines() if ln.strip()]
    for ep in episodes:
        tag = {"rework": "reworked", "escalation": "escalated"}.get(ep["kind"], "did")
        lines.append(f"- {tag}: {ep['gist']}")
    cap = int(tuning.get("home_memory_char_cap"))
    db.upsert_memory(home_id, "summary", "\n".join(lines)[-cap:])


async def consolidate(home_id: int, settings: dict, *, allow_spend: bool = True) -> dict[str, Any]:
    """Fold one agent's accumulated episodes into long-term memory.

    Returns {folded, spent, note}. `spent` is the count of model calls made (0 or
    1), so the caller can hold it against the daily budget and the tests can prove
    the cost. `allow_spend=False` forces the deterministic floor — used when the
    daily ceiling is reached.
    """
    episodes = db.unconsolidated(home_id)
    if not episodes:
        return {"folded": 0, "spent": 0, "note": "nothing to fold"}

    ids = [e["id"] for e in episodes]
    if not allow_spend:
        _truncate_fold(home_id, episodes)
        db.mark_consolidated(ids)
        return {"folded": len(ids), "spent": 0, "note": "truncated (spend not allowed)"}

    prompt = (
        "EXISTING MEMORY:\n" + (current_blob(home_id) or "(none yet)") +
        "\n\nNEW EVENTS (newest last):\n" +
        "\n".join(f"- [{e['kind']}] {e['gist']}" for e in episodes))
    model = tuning.get("home_compress_model")
    try:
        raw = await providers.complete(
            "anthropic", model, SYSTEM, prompt, settings,
            max_tokens=int(tuning.get("home_compress_max_tokens")))
    except Exception as e:
        # The floor. A failed fold must not lose episodes or stall memory — write
        # the deterministic version and mark them done so we do not retry forever.
        _truncate_fold(home_id, episodes)
        db.mark_consolidated(ids)
        bus.emit(0, None, "system", "home_consolidate_fallback",
                 {"home": home_id, "reason": str(e)[:200]})
        return {"folded": len(ids), "spent": 0, "note": f"model failed, truncated: {e}"}

    sections = _parse(raw)
    if not sections:
        _truncate_fold(home_id, episodes)
        db.mark_consolidated(ids)
        return {"folded": len(ids), "spent": 1, "note": "unparseable, truncated"}

    # Additive-then-swap: write the summary FIRST, mark consolidated SECOND. If the
    # process dies between them the episodes stay unconsolidated and are re-folded,
    # which is a harmless repeat rather than a silent loss.
    cap = int(tuning.get("home_memory_char_cap"))
    for section in SECTIONS:
        text = str(sections.get(section, "")).strip()
        if text:
            db.upsert_memory(home_id, section, text[:cap])
    db.mark_consolidated(ids)
    db.update_home_agent(home_id, last_consolidated_at=_now())
    return {"folded": len(ids), "spent": 1, "note": "consolidated"}


def _parse(raw: str) -> dict:
    text = (raw or "").strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _now() -> float:
    import time
    return time.time()
