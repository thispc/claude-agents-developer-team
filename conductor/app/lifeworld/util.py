"""Small shared numerics and a keyed seal, in one place so every subsystem agrees.

The seal is the artifact-secret model, self-contained (the Lifeworld does not borrow the
projects engine's crypto): a sha256 keystream XORed over the plaintext. It is a faithful
*model* of secrecy — the stored bytes genuinely don't contain the plaintext and only the
key reverses them — not a claim to defeat a database admin. For the real threat (one agent
must not read another's sealed value) that is exactly the right shape.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets as _secrets
from typing import Any


def clamp01(x: float) -> float:
    """Every state variable lives in [0, 1]; NaN collapses to the floor so one bad
    number can never poison an entity."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    if x != x:                       # NaN
        return 0.0
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def step(current: float, delta: float, cap: float) -> float:
    """Add a delta bounded to ±cap, then clamp to [0, 1]. The anti-crazy guard: no
    single event moves a variable more than its family's cap."""
    return clamp01(current + max(-cap, min(cap, float(delta))))


def saturating(x: float, k: float = 0.15) -> float:
    """1 − e^(−k·x): the diminishing-returns curve behind skill growth and maturation."""
    return 1.0 - math.exp(-k * max(0.0, x))


def sha(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()


def new_key() -> str:
    return _secrets.token_hex(16)


def _keystream(key: str, n: int) -> bytes:
    out = bytearray()
    c = 0
    while len(out) < n:
        out.extend(hashlib.sha256(f"{key}:{c}".encode()).digest())
        c += 1
    return bytes(out[:n])


def seal(value: Any, key: str) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.b64encode(bytes(b ^ k for b, k in zip(raw, _keystream(key, len(raw))))).decode()


def unseal(cipher: str, key: str) -> Any:
    if not cipher:
        return None
    raw = base64.b64decode(cipher.encode())
    return json.loads(bytes(b ^ k for b, k in zip(raw, _keystream(key, len(raw)))).decode())
