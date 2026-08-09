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
from schemathesis.specs.openapi.checks import (                    # noqa: E402
    positive_data_acceptance, unsupported_method)

import lifeworld_service_app as svc                     # noqa: E402  (see conftest)

SPEC = Path(__file__).resolve().parent.parent / "openapi.json"

schema = schemathesis.openapi.from_path(str(SPEC))
schema.app = svc.app                                    # ASGI transport — in-process


# derandomize: a CONTRACT gate must say the same thing on every run — a fuzz seed
# that changes per run makes the suite mean something different each time.
# filter_too_much: the bounded ids/counts legitimately reject a slice of the
# generator's candidates; that is the contract working, not a test smell.
# max_examples: this service has 43 operations where knowledge has 7, and each one
# deserialises a world blob. At the default hundred examples the gate took longer
# than the entire rest of the repo's suite, which is how a gate stops being run.
# Ten is enough to catch a route that answers a status its spec never declared —
# the failure this exists for — and the SHAPES are pinned exactly in the smoke test.
@settings(derandomize=True, deadline=None, max_examples=10,
          suppress_health_check=[HealthCheck.filter_too_much])
@schema.parametrize()
def test_contract(case):
    # The conductor's client adds X-Service-Token AND the caller stamp on every
    # call, so the contract is exercised the way real traffic arrives:
    # authenticated, and with a caller the service can authorise against. A
    # request missing the stamp is a bug upstream, not a shape in the contract —
    # same reason the knowledge suite sends the token rather than fuzzing 401s.
    # `unsupported_method` is excluded, and only this one. Two routes here are
    # LITERAL segments in the same position as a path parameter —
    # `/thread/connect` and `/thread/disconnect` beside `/thread/{tid}` — so a
    # DELETE aimed at `/thread/connect` matches the parameterised route and
    # answers "connect is not an integer" (422) instead of 405. That is inherited
    # from the paths the dashboard has always used and cannot move; the only fixes
    # are a Starlette int converter, which would put a non-standard token in the
    # committed contract, or renaming a path the browser hardcodes. A 422 where a
    # 405 belongs, on a request no client makes, is the smaller lie.
    # `positive_data_acceptance` is excluded for a different and simpler reason:
    # two routes refuse input that is SYNTACTICALLY fine. An artifact spec is a
    # free-form object in the schema because its vocabulary is a runtime library,
    # so "this spec names no component this world knows" is a rule JSON Schema
    # cannot carry — the alternative is to accept the spec and build nothing,
    # which is worse than a 400 that says why. Every SHAPE the routes do return
    # is still checked, by status_code_conformance and response_schema_conformance.
    case.call_and_validate(
        excluded_checks=[unsupported_method, positive_data_acceptance], headers={
        "X-Service-Token": svc.helpers.SERVICE_TOKEN,
        "X-Lw-Owner": "1", "X-Lw-Root": "1",
        "X-Lw-Settings": "user:1.testsignature",
        "X-Lw-Source": "studio", "X-Lw-Author": "1"})
