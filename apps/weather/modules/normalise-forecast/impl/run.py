# A Python implementation of the normalise-forecast contract.
#
# Not a port. It satisfies the same interface.json and answers the same
# conformance.json as the JavaScript implementation, so the ledger holds both for
# one contract and which is live is a decision rather than a fact about the code.
#
# Written from the contract, which is why the rounding below is spelled out
# rather than delegated: Python's built-in round() is banker's rounding, so
# round(2.5) is 2 and round(-2.5) is -2. The contract says half AWAY FROM ZERO,
# and two of the conformance cases exist to catch exactly this.

import math

# WMO weather interpretation codes, collapsed to the seven words this app uses.
SKY_BANDS = [
    (0, "clear"),
    (3, "cloud"),
    (48, "fog"),    # 45, 48 — fog and depositing rime fog
    (67, "rain"),   # 51-67 — drizzle and rain, freezing included
    (77, "snow"),   # 71-77 — snow fall and snow grains
    (82, "rain"),   # 80-82 — rain showers
    (86, "snow"),   # 85, 86 — snow showers
    (99, "storm"),  # 95-99 — thunderstorm
]

COLUMNS = {
    "date": "time",
    "code": "weather_code",
    "high": "temperature_2m_max",
    "low": "temperature_2m_min",
    "precip": "precipitation_sum",
}


class _Refusal(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.code = "EBADPAYLOAD"


def normalise(payload_in):
    if not isinstance(payload_in, dict):
        raise _Refusal("input must be an object")

    name = payload_in.get("name")
    payload = payload_in.get("payload")
    if not isinstance(name, str) or name.strip() == "":
        raise _Refusal("name must be a non-empty string")
    if not isinstance(payload, dict):
        raise _Refusal("payload must be an object")

    lat = payload.get("latitude")
    lon = payload.get("longitude")
    if not _is_number(lat) or not _is_number(lon):
        raise _Refusal("payload is missing latitude/longitude")

    daily = payload.get("daily")
    if not isinstance(daily, dict):
        raise _Refusal("payload has no daily block")

    cols = {}
    for key, field in COLUMNS.items():
        col = daily.get(field)
        if not isinstance(col, list):
            raise _Refusal(f"payload.daily.{key} is missing or not an array")
        cols[key] = col

    # Ragged columns are the failure mode of every column-oriented API: one array
    # comes back short and a naive reader emits days with missing fields.
    n = len(cols["date"])
    for key, col in cols.items():
        if len(col) != n:
            raise _Refusal(f"payload.daily.{key} has {len(col)} entries but time has {n} — the columns do not line up")

    days = []
    for i in range(n):
        date = cols["date"][i]
        if not isinstance(date, str):
            raise _Refusal(f"day {i}: time is not a string")
        days.append({
            "date": date,
            "highC": int(_round_half(_num(cols["high"][i], f"day {i}: temperature_2m_max"), 0)),
            "lowC": int(_round_half(_num(cols["low"][i], f"day {i}: temperature_2m_min"), 0)),
            "precipMm": _round_half(_num(cols["precip"][i], f"day {i}: precipitation_sum"), 1),
            "sky": _sky_of(cols["code"][i]),
        })

    return {"place": {"name": name.strip(), "lat": lat, "lon": lon}, "days": days}


def _round_half(value, places):
    """Round half AWAY FROM ZERO — NOT Python's round(), which is banker's.

    round(2.5) is 2 here and 3 in JavaScript. Two honest implementations of this
    contract would differ by a degree and nothing would explain why, so the rule
    is written out and pinned by conformance cases at both boundaries.
    """
    scale = 10 ** places
    scaled = value * scale
    sign = -1 if scaled < 0 else 1
    r = sign * math.floor(abs(scaled) + 0.5)
    out = r / scale
    # Integers must serialise as integers: 0.0 renders as "0.0" in JSON and would
    # not equal the 0 the contract asks for.
    return out + 0 if places > 0 else int(out)


def _sky_of(code):
    if not _is_number(code):
        return "unknown"
    for limit, sky in SKY_BANDS:
        if code <= limit:
            return sky
    return "unknown"


def _num(v, where):
    if not _is_number(v):
        raise _Refusal(f"{where} is not a finite number")
    return v


def _is_number(v):
    # bool is a subclass of int in Python and is not a number in JSON's sense.
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
