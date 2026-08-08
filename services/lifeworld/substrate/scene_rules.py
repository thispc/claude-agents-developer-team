"""Scene rules — ordered typed rows, like AWS ingress rules.

A scene's standing rules are a list of fixed-schema rows evaluated top to bottom. Each row is
data selecting + parameterising vetted code — NEVER a string that is exec'd, exactly as the
habit compiler (rules.py) does it. Three effect families bind on the three seams a beat already
crosses, so a rule shapes behaviour on BOTH the free path and the paid one:

  · GATE   (allow / deny)      first match wins — decides whether the beat happens at all,
                               in Scene.deliver, BEFORE anyone perceives (a denied beat spends nothing).
  · SHAPE  (clamp / bias)      all apply — bound/nudge the resulting Packet deltas in World.appraise,
                               AFTER the appraiser returns, so the code disposes even if the model ignored a rule.
  · PROMPT (annotate)          compiled into the numbered rules block handed to the model.

The evaluator only ever reads whitelisted fields and writes whitelisted Packet families, so a
rule can neither read nor rewrite a holder-sealed secret. Unknown effects/keys are inert.
"""

from __future__ import annotations

from typing import Any

from .types import Packet, Signal

GATE_EFFECTS = frozenset({"allow", "deny"})
SHAPE_EFFECTS = frozenset({"clamp", "bias"})
PROMPT_EFFECTS = frozenset({"annotate"})
ALL_EFFECTS = GATE_EFFECTS | SHAPE_EFFECTS | PROMPT_EFFECTS

# Which signal/ctx keys a `when` may match on (typed subset-match, same semantics as rules.py).
WHEN_KEYS = frozenset({"kind", "tone", "from_trusted", "domain"})

# The ONLY Packet fields a SHAPE effect may touch. Relationship (social) deltas are deliberately
# out of scene-rule range for v1, and nothing holder-scoped is ever reachable.
SHAPE_FIELDS: dict[str, frozenset | None] = {
    "mood": frozenset({"confidence", "stress", "hope", "focus"}),
    "vitals": frozenset({"energy", "health"}),
    "drives": None,        # any drive key
}

MAX_ROWS = 32


def _match(when: dict, signal: Signal, ctx: dict) -> bool:
    """Every key in `when` must equal the corresponding signal/ctx value (subset match)."""
    for k, want in when.items():
        if k not in WHEN_KEYS:
            return False                    # an unknown key never matches (inert, not a wildcard)
        got = ctx.get(k, getattr(signal, k, signal.payload.get(k)))
        if got != want:
            return False
    return True


def _field(spec: str):
    """Resolve 'mood.stress' -> ('mood', 'stress') iff whitelisted, else (None, None)."""
    if not spec or "." not in spec:
        return None, None
    fam, key = spec.split(".", 1)
    allowed = SHAPE_FIELDS.get(fam, False)
    if allowed is False:
        return None, None
    if allowed is not None and key not in allowed:
        return None, None
    return fam, key


class SceneRuleSet:
    def __init__(self, rows: list[dict] | None = None, note: str = ""):
        self.rows: list[dict] = rows or []
        self.note: str = note or ""

    # --- seam 1: the gate (Scene.deliver) -----------------------------------
    def gate(self, signal: Signal, ctx: dict) -> bool:
        """True = let the beat happen; False = block it. First matching allow/deny row decides;
        default allow when nothing matches."""
        for r in self.rows:
            eff = r.get("effect")
            if eff in GATE_EFFECTS and _match(r.get("when", {}), signal, ctx):
                return eff == "allow"
        return True

    # --- seam 2: the shape (World.appraise, after the appraiser) -------------
    def shape(self, packet: Packet, signal: Signal | None = None, ctx: dict | None = None) -> Packet:
        """Clamp/bias the packet's deltas, deterministically. Only whitelisted fields move."""
        ctx = ctx or {}
        for r in self.rows:
            eff = r.get("effect")
            if eff not in SHAPE_EFFECTS:
                continue
            if signal is not None and r.get("when") and not _match(r["when"], signal, ctx):
                continue
            fam, key = _field(str(r.get("field", "")))
            if fam is None:
                continue
            d = getattr(packet, fam, None)
            if not isinstance(d, dict):
                continue
            try:
                v = float(r.get("value", 0))
            except (TypeError, ValueError):
                continue
            if eff == "clamp":
                lo, hi = (-abs(v), abs(v))
                if key in d:
                    d[key] = max(lo, min(hi, d[key]))
            elif eff == "bias":
                d[key] = d.get(key, 0.0) + v
        return packet

    # --- seam 3: the compiled prompt block (appraise.model) -----------------
    def as_prompt(self) -> str:
        lines = []
        for r in self.rows:
            note = (r.get("note") or "").strip()
            if r.get("effect") in PROMPT_EFFECTS and note:
                lines.append(note[:120])
            elif note:                                  # any row's note is guidance for the model too
                lines.append(note[:120])
        block = "; ".join(f"{i + 1}. {t}" for i, t in enumerate(lines))
        note = self.note.strip()
        if note:
            block = (block + " " if block else "") + note
        return block[:600]

    def to_list(self) -> list[dict]:
        return list(self.rows)


def validate_rows(raw: Any) -> list[dict]:
    """The trust boundary: coerce client-supplied rows to a safe, typed, capped list. Unknown
    effects/keys/fields are dropped, params are scalar-coerced, and the list is renumbered."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_ROWS]:
        if not isinstance(item, dict):
            continue
        eff = item.get("effect")
        if eff not in ALL_EFFECTS:
            continue
        when = {}
        for k, val in (item.get("when") or {}).items():
            if k in WHEN_KEYS and isinstance(val, (str, int, float, bool)):
                when[k] = val
        row: dict[str, Any] = {"n": len(out), "effect": eff, "when": when,
                               "note": str(item.get("note", ""))[:160]}
        if eff in SHAPE_EFFECTS:
            fam, key = _field(str(item.get("field", "")))
            if fam is None:
                continue                                # a shape row with no valid field is meaningless
            row["field"] = f"{fam}.{key}"
            try:
                row["value"] = round(float(item.get("value", 0)), 4)
            except (TypeError, ValueError):
                row["value"] = 0.0
        out.append(row)
    return out
