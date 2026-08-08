"""Contract: the COMMITTED openapi.json property-tested against the live app.

Schemathesis loads the committed spec (never the app's opinion of itself) and
drives the ASGI app in-process — offline, no sockets. Every operation in the
contract is exercised with generated inputs; a route that drifted from the spec
fails here before oasdiff ever sees a diff.
"""

from pathlib import Path

import pytest

schemathesis = pytest.importorskip("schemathesis")

import helpers                                          # noqa: E402
from app import app                                     # noqa: E402

SPEC = Path(__file__).resolve().parent.parent / "openapi.json"

schema = schemathesis.openapi.from_path(str(SPEC))
schema.app = app                                        # ASGI transport — in-process


@schema.parametrize()
def test_contract(case):
    # The gateway adds X-Service-Token server-side on every proxied call, so the
    # contract is exercised the way real traffic arrives: authenticated.
    case.call_and_validate(headers={"X-Service-Token": helpers.SERVICE_TOKEN})
