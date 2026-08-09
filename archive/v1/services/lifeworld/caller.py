"""Who is asking — the conductor's stamp, and the ownership gate it feeds.

Authentication happened conductor-side, against a session cookie this service is
deliberately never shown (the gateway strips it). What arrives here is the
identity the conductor VOUCHED FOR on a request it had already authenticated,
carried in five headers, plus the service token that proves the request came
through the conductor at all:

    X-Lw-Owner     the user id whose worlds these are           (required)
    X-Lw-Root      "1" when the caller is the operator account  — root sees an
                   agent's private decisions; nobody else does
    X-Lw-Settings  the OPAQUE settings reference the model door resolves
                   ("user:7", "root"); blank means no credentials, so live mode
                   is refused rather than attempted
    X-Lw-Source    who to bill on the shared quota meter (studio | repair) —
                   explicit on the wire since P2, never guessed downstream
    X-Lw-Author    "1" when this caller may spend on AUTHORING (the owner has
                   their own credentials); the conductor knows, this service
                   cannot and must not

OWNERSHIP is enforced in this service, not in the proxy, because the `owner_id`
column is in this service. The conductor authenticates; the row authorises. A
check made against a copy of a table you no longer own is a check against a
stale copy, and after P4-B there is no other copy at all.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException

import helpers


class Principal:
    def __init__(self, owner_id: int, is_root: bool, settings_ref: str,
                 source: str, can_author: bool):
        self.owner_id = int(owner_id or 0)
        self.is_root = bool(is_root)
        self.settings_ref = settings_ref or ""
        self.source = source or "studio"
        self.can_author = bool(can_author)

    @property
    def live_ok(self) -> bool:
        """Live mode needs somewhere for the model door to resolve credentials.
        With no reference the world loads FREE — the deterministic appraiser —
        which is the same answer the conductor gave when a user had no key."""
        return bool(self.settings_ref)


def principal(x_lw_owner: str | None = Header(None),
              x_lw_root: str | None = Header(None),
              x_lw_settings: str | None = Header(None),
              x_lw_source: str | None = Header(None),
              x_lw_author: str | None = Header(None)) -> Principal:
    try:
        owner = int(x_lw_owner or 0)
    except ValueError:
        owner = 0
    if owner <= 0:
        raise HTTPException(400, "no caller stamped on this request — the conductor "
                                 "sets X-Lw-Owner on every forwarded call")
    return Principal(owner, (x_lw_root or "") == "1", (x_lw_settings or "").strip(),
                     (x_lw_source or "studio").strip(), (x_lw_author or "") == "1")


# Every route beyond /health and /openapi.json (SERVICE_CONTRACT rule 3).
AUTH = [Depends(helpers.require_token)]
