"""The artifact library and the object model — how a thing is modelled as code.

This is the owner's card, made concrete. An artifact has two halves:

- a **public** half — variables anyone in the scene can read without interacting
  (its kind, its shown face when up, its position);
- a **secret** half — variables that are *encrypted* and unreadable from the stored
  row alone. The value (ace of diamonds) is sealed with a **key**; the ciphertext
  sits on the artifact, and the key is held privately by whichever agent possesses
  the thing. Only interaction hands the key over, and only the key decrypts.

So "a folded card hides its value" is not a flag we trust — it is literally
unreadable: the row holds ciphertext, the holder holds the key, and `reveal` is the
one function that brings them together. An agent with the key sees the value; anyone
reading the scene sees only that a sealed secret exists. This is the private-pool
idea the owner described: public variables are shared for free, secret ones only
after an interaction places the key in an agent's private hands.

Keys live in the agent's own `scene_agents.private_state` — the same isolated pool a
poker hand already lives in, already excluded from every other view — so the object
model reuses the tested secret-isolation of the scene engine rather than inventing a
second one.

Wave 1 is the model and the library CRUD. The *behaviour* of an active artifact — the
code that runs on interaction, the LLM that decides — is wave 2; this is the shape it
will plug into. No artifact executes code here; a def is data.
"""

import base64
import hashlib
import json
import secrets
from typing import Any

from . import db


# --- the cipher: a keyed stream, dependency-free ------------------------------
#
# A sha256 keystream XORed over the plaintext. This is a faithful *model* of
# encryption — the stored ciphertext genuinely does not contain the plaintext, and
# only the key reverses it — not a claim of production-grade cryptography. It exists
# to make "hidden until revealed" a real property of the bytes, not a trusted flag.

def new_key() -> str:
    return secrets.token_hex(16)


def _keystream(key: str, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(hashlib.sha256(f"{key}:{counter}".encode()).digest())
        counter += 1
    return bytes(out[:n])


def encrypt(plaintext: str, key: str) -> str:
    raw = plaintext.encode("utf-8")
    xored = bytes(b ^ k for b, k in zip(raw, _keystream(key, len(raw))))
    return base64.b64encode(xored).decode("ascii")


def decrypt(cipher: str, key: str) -> str:
    raw = base64.b64decode(cipher.encode("ascii"))
    return bytes(b ^ k for b, k in zip(raw, _keystream(key, len(raw)))).decode("utf-8")


def seal(values: dict, key: str) -> str:
    """Encrypt a bag of secret variables into one ciphertext string."""
    return encrypt(json.dumps(values, separators=(",", ":")), key)


def unseal(cipher: str, key: str) -> dict:
    """Decrypt a sealed secret back into variables. Raises on the wrong key rather
    than returning garbage — a failed reveal is a refusal, not a silent misread."""
    if not cipher:
        return {}
    data = json.loads(decrypt(cipher, key))
    return data if isinstance(data, dict) else {}


# --- the library: reusable object blueprints ---------------------------------

def create_def(owner_id: int, name: str, kind: str = "prop", *, dormant: bool = True,
               public: dict | None = None, secret_schema: list | None = None,
               description: str = "") -> dict:
    did = db.create_artifact_def(owner_id, name.strip() or kind, kind,
                                 dormant=dormant, public=public,
                                 secret_schema=secret_schema, description=description)
    return _hydrate(db.get_artifact_def(did))


def list_defs(owner_id: int) -> list[dict]:
    return [_hydrate(d) for d in db.list_artifact_defs(owner_id)]


def get_def(def_id: int) -> dict | None:
    d = db.get_artifact_def(def_id)
    return _hydrate(d) if d else None


def owns_def(owner_id: int, def_id: int) -> bool:
    d = db.get_artifact_def(def_id)
    return bool(d and d["owner_id"] == owner_id)


def _hydrate(d: dict | None) -> dict | None:
    if not d:
        return d
    return {**d,
            "public": _loads(d.get("public"), {}),
            "secret_schema": _loads(d.get("secret_schema"), []),
            "dormant": bool(d.get("dormant", 1))}


# --- placing an object into a scene, sealing its secret ----------------------

def place(scene_id: int, def_id: int | None, *, kind: str = "prop",
          public: dict | None = None, secret: dict | None = None,
          holder_seat: int | None = None, dormant: bool = True) -> dict:
    """Drop an artifact into a scene. Its public variables are stored in the clear;
    its secret variables (if any) are SEALED with a fresh key, and that key is handed
    to the holder seat's private pool — so from this moment the value is unreadable to
    anyone but the holder, exactly as a dealt, face-down card should be."""
    cipher = ""
    if secret:
        key = new_key()
        cipher = seal(secret, key)
        if holder_seat is not None:
            grant_key(holder_seat, "pending", key)   # keyed by artifact id once created
    aid = db.create_artifact(scene_id, kind, public or {},
                             visibility="facedown" if secret else "public",
                             holder=holder_seat, def_id=def_id,
                             dormant=dormant, secret=cipher)
    if secret and holder_seat is not None:
        _rekey_pending(holder_seat, aid)             # move the key under the real id
    return db.get_artifact(aid)


def grant_key(seat_id: int, artifact_id: Any, key: str) -> None:
    """Put a key into an agent's private pool. Stored inside scene_agents.private_state
    — the same isolated blob a hand lives in — so no other agent's view can read it."""
    seat = db.get_scene_agent(seat_id)
    if not seat:
        return
    priv = _loads(seat.get("private_state"), {})
    priv.setdefault("keys", {})[str(artifact_id)] = key
    db.update_scene_agent(seat_id, private_state=json.dumps(priv))


def _rekey_pending(seat_id: int, artifact_id: int) -> None:
    seat = db.get_scene_agent(seat_id)
    priv = _loads((seat or {}).get("private_state"), {})
    keys = priv.get("keys", {})
    if "pending" in keys:
        keys[str(artifact_id)] = keys.pop("pending")
        db.update_scene_agent(seat_id, private_state=json.dumps(priv))


def key_held_by(seat_id: int, artifact_id: int) -> str | None:
    seat = db.get_scene_agent(seat_id)
    priv = _loads((seat or {}).get("private_state"), {})
    return priv.get("keys", {}).get(str(artifact_id))


def reveal(artifact_id: int, seat_id: int) -> dict | None:
    """The interaction that unlocks a secret: if this seat holds the key, decrypt and
    return the artifact's secret variables; otherwise None. This is the ONLY path from
    ciphertext to value, and it needs a key an agent was actually given."""
    art = db.get_artifact(artifact_id)
    if not art or not art.get("secret"):
        return None
    key = key_held_by(seat_id, artifact_id)
    if not key:
        return None
    try:
        return unseal(art["secret"], key)
    except (ValueError, json.JSONDecodeError):
        return None


def public_of(artifact: dict) -> dict:
    """What anyone may read about an artifact — its public variables, never the
    sealed secret. The scene view is built from this."""
    return {"id": artifact["id"], "type": artifact["type"], "def_id": artifact.get("def_id"),
            "dormant": bool(artifact.get("dormant", 1)),
            "public": _loads(artifact.get("state"), {}),
            "sealed": bool(artifact.get("secret")),      # a secret EXISTS, but not its value
            "holder": artifact.get("holder")}


def _loads(blob: Any, default: Any) -> Any:
    if isinstance(blob, (dict, list)):
        return blob
    try:
        return json.loads(blob or ("{}" if isinstance(default, dict) else "[]"))
    except (ValueError, TypeError):
        return default
