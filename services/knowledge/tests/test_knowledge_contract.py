"""Contract: the COMMITTED openapi.json property-tested against the live app.

Schemathesis loads the committed spec (never the app's opinion of itself) and
drives the ASGI app in-process — offline, no sockets. Every operation in the
contract is exercised with generated inputs; a route that drifted from the spec
fails here before oasdiff ever sees a diff.
"""

from pathlib import Path

import pytest

schemathesis = pytest.importorskip("schemathesis")

from hypothesis import HealthCheck, settings            # noqa: E402

import knowledge_service_app as svc                     # noqa: E402  (see conftest)

SPEC = Path(__file__).resolve().parent.parent / "openapi.json"

schema = schemathesis.openapi.from_path(str(SPEC))
schema.app = svc.app                                    # ASGI transport — in-process


# derandomize: a CONTRACT gate must say the same thing on every run — a fuzz seed
# that changes per run makes the suite mean something different each time.
# filter_too_much: the bounded ids/counts legitimately reject a slice of the
# generator's candidates; that is the contract working, not a test smell.
@settings(derandomize=True, deadline=None,
          suppress_health_check=[HealthCheck.filter_too_much])
@schema.parametrize()
def test_contract(case):
    # The conductor's shim adds X-Service-Token on every call, so the contract is
    # exercised the way real traffic arrives: authenticated.
    case.call_and_validate(headers={"X-Service-Token": svc.helpers.SERVICE_TOKEN})
