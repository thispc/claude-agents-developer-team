# A Python implementation of the advise-clothing contract.
#
# The same thresholds, because they are the contract rather than an
# implementation choice — the conformance suite pins every boundary, including
# the ones just either side of it.

import math

COAT_BELOW_C = 5
HAT_ABOVE_C = 25
UMBRELLA_AT_MM = 1


class _Refusal(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.code = "EBADINPUT"


def advise(payload):
    if not isinstance(payload, dict):
        raise _Refusal("input must be an object")
    forecast = payload.get("forecast")
    if not isinstance(forecast, dict):
        raise _Refusal("forecast must be an object")
    days = forecast.get("days")
    if not isinstance(days, list):
        raise _Refusal("forecast.days must be an array")

    items = set()
    coldest = math.inf
    warmest = -math.inf

    for i, day in enumerate(days):
        if not isinstance(day, dict):
            raise _Refusal(f"day {i} is not an object")
        low = _num(day.get("lowC"), f"day {i}: lowC")
        high = _num(day.get("highC"), f"day {i}: highC")
        precip = _num(day.get("precipMm"), f"day {i}: precipMm")

        coldest = min(coldest, low)
        warmest = max(warmest, high)

        if low < COAT_BELOW_C:
            items.add("a warm coat")
        if high > HAT_ABOVE_C:
            items.add("a sun hat")
        if precip >= UMBRELLA_AT_MM:
            items.add("an umbrella")
        if day.get("sky") == "snow":
            items.add("boots")
        if day.get("sky") == "storm":
            items.add("a reason to stay in")

    # sorted() on strings compares by code point, which is what the contract
    # asks for. Anything locale-aware would read LANG and the determinism gate
    # would refuse it — correctly.
    ordered = sorted(items)

    return {"items": ordered, "summary": _summarise(len(days), ordered, coldest, warmest)}


def _summarise(count, items, coldest, warmest):
    if count == 0:
        return "No forecast to advise on."
    span = "Today" if count == 1 else f"Over the next {count} days"
    rng = f"{_deg(coldest)}° to {_deg(warmest)}°"
    if not items:
        return f"{span}: {rng}. Take nothing special."
    return f"{span}: {rng}. Take {_list(items)}."


def _deg(v):
    """Whole degrees render without a trailing .0 — the contract's strings say
    "12°", and Python would otherwise write "12.0°" for a float that arrived
    from JSON."""
    return int(v) if float(v).is_integer() else v


def _list(xs):
    if len(xs) == 1:
        return xs[0]
    return ", ".join(xs[:-1]) + " and " + xs[-1]


def _num(v, where):
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        raise _Refusal(f"{where} is not a finite number")
    return v
