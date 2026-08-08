"""The shims must not build an httpx client per call — and the pool must not
become the classic bug that replaces it.

WHY THIS FILE EXISTS. Every service shim used to open a brand new client per call.
Measured against the live fleet: 5.63ms for a fresh client versus 0.56ms on a
pooled connection, so `/api/graph/self` — ~43 service calls — spent ~90% of its
219ms on connection setup nobody asked for. app/pool.py shares the clients.

Sharing an httpx client is not free of hazards, and both of them fail SILENTLY,
which is why they are drilled here rather than left to review:

  * a cached client keeps the transport and URL it was born with, so a test that
    points a shim at a mounted service (or at a transport that refuses) would
    quietly go on talking to the previous one — the drill would still pass, against
    the wrong thing;
  * an httpx.AsyncClient binds its connection pool to the loop that created it, so
    a client cached across `asyncio.run()` calls fails in ways that read as network
    trouble. The conductor has one loop; pytest-asyncio has one per test.

So: a structural pin that no shim constructs a client, a construction count, an
invalidation drill, a two-loops drill, and the outage/recovery round trip.
"""

import asyncio
import re
from pathlib import Path

import httpx
import pytest

from conftest import knowledge_service

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "conductor" / "app"

# Every module that talks to another process in the fleet on a hot path.
SHIMS = ["knowledge.py", "usage.py", "notify.py", "logs.py", "monitor.py",
         "lifeworld_client.py", "modgraph.py", "fleet.py", "routes/svc.py"]


class _DeadTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """The service being down, as a transport: every request refuses."""

    def handle_request(self, request):
        raise httpx.ConnectError("connection refused (drill)")

    async def handle_async_request(self, request):
        raise httpx.ConnectError("connection refused (drill)")


# --- the pin: no shim builds a client ----------------------------------------

def test_no_shim_constructs_an_httpx_client():
    """THE POOLING PIN, stated where it cannot be satisfied by accident.

    Not "the shims are fast" (a timing assertion on a shared laptop is a flake) and
    not "one client exists" (a shim could still build a second one on a branch
    nobody runs). The claim is structural: the ONLY place in the conductor that
    constructs an httpx client for a fleet peer is app/pool.py, so a new verb — or
    a merge that resurrects the old shape — cannot reintroduce the tax quietly.
    """
    offenders = []
    for name in SHIMS:
        src = (APP / name).read_text()
        # strings/comments excluded by requiring the constructor to be CALLED
        for m in re.finditer(r"\bhttpx\.(Async)?Client\s*\(", src):
            line = src[:m.start()].count("\n") + 1
            offenders.append(f"{name}:{line}")
    assert not offenders, (
        "these built their own httpx client instead of asking app/pool.py — a "
        f"fresh client is ~5ms of setup on a ~0.5ms call: {offenders}")


def test_every_shim_asks_the_pool():
    """...and the other half of the pin: they all actually go through it. A shim
    that stopped calling pool.* would satisfy the test above by making no HTTP
    calls at all, which is not the property being protected."""
    for name in SHIMS:
        src = (APP / name).read_text()
        assert re.search(r"pool\.(a)?sync_client\(|pool\.async_client\(", src), \
            f"{name} reaches the fleet without the shared client pool"


# --- the pool itself ---------------------------------------------------------

def test_the_pool_constructs_one_client_however_often_it_is_asked(monkeypatch):
    """Counted at the constructor, because "is the same object" would also be true
    of a cache that rebuilt and got lucky with an address."""
    from app import pool

    built = []
    real = httpx.Client

    class _Counting(real):
        def __init__(self, *a, **k):
            built.append(1)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "Client", _Counting)
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    first = pool.sync_client("drill:count", base_url="http://drill.test",
                             timeout=1.0, transport=t)
    for _ in range(50):
        assert pool.sync_client("drill:count", base_url="http://drill.test",
                                timeout=1.0, transport=t) is first
    assert sum(built) == 1, f"{sum(built)} clients for 51 calls"
    first.close()


async def test_the_pool_constructs_one_async_client_per_loop(monkeypatch):
    from app import pool

    built = []
    real = httpx.AsyncClient

    class _Counting(real):
        def __init__(self, *a, **k):
            built.append(1)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", _Counting)
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    first = pool.async_client("drill:acount", base_url="http://drill.test",
                              timeout=1.0, transport=t)
    for _ in range(50):
        assert pool.async_client("drill:acount", base_url="http://drill.test",
                                 timeout=1.0, transport=t) is first
    assert sum(built) == 1, f"{sum(built)} clients for 51 calls"
    await first.aclose()


def test_a_swapped_transport_rebuilds_the_client():
    """The reason the cache is keyed on the CONFIGURATION and not on a bare name.

    Every outage drill in this suite works by swapping a transport in. If the pool
    handed back the client built with the previous one, those drills would pass
    while testing nothing — the worst failure mode a test can have.
    """
    from app import pool
    a = httpx.MockTransport(lambda r: httpx.Response(200, json={"n": 1}))
    b = httpx.MockTransport(lambda r: httpx.Response(200, json={"n": 2}))
    c1 = pool.sync_client("drill:swap", base_url="http://drill.test", transport=a)
    assert pool.sync_client("drill:swap", base_url="http://drill.test",
                            transport=a) is c1
    c2 = pool.sync_client("drill:swap", base_url="http://drill.test", transport=b)
    assert c2 is not c1, "the client survived a transport swap"
    assert c2.get("/x").json()["n"] == 2, "and it is still pointed at the old one"
    # ...and the same for the URL, and for a changed token header.
    c3 = pool.sync_client("drill:swap", base_url="http://other.test", transport=b)
    assert c3 is not c2
    c4 = pool.sync_client("drill:swap", base_url="http://other.test", transport=b,
                          headers={"X-Service-Token": "rotated"})
    assert c4 is not c3
    c4.close()


def test_a_closed_client_is_rebuilt_rather_than_raised_through():
    """Shutdown closes the pool. A shim that logs on the way out — or a test that
    ran after one — must get a working client back, not "client has been closed"
    from inside a degraded path whose whole job is not to raise."""
    from app import pool
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    c1 = pool.sync_client("drill:closed", base_url="http://drill.test", transport=t)
    c1.close()
    c2 = pool.sync_client("drill:closed", base_url="http://drill.test", transport=t)
    assert c2 is not c1 and not c2.is_closed
    assert c2.get("/x").status_code == 200
    c2.close()


# --- the shims, live against the mounted services ----------------------------

async def test_the_async_shims_hand_back_one_client(fresh_db):
    """knowledge, notify and the lifeworld door: same object, call after call, on
    one loop."""
    from app import knowledge, lifeworld_client, notify
    for shim in (knowledge, notify, lifeworld_client):
        assert shim._client() is shim._client(), \
            f"{shim.__name__} built a second client on the same loop"


async def test_pooling_did_not_cost_the_round_trip(fresh_db):
    """The point of all this is that the shim still WORKS. Remember and recall,
    through the shared client, against the mounted service."""
    from app import knowledge
    assert await knowledge.remember("a1", "the pooled client still writes",
                                    "it does", good=1) > 0
    assert await knowledge.recall("a1", "pooled client still writes", k=1) != []


# --- hazard 2: the event loop ------------------------------------------------

def test_two_event_loops_never_share_a_client(fresh_db):
    """THE CLASSIC BUG THIS CHANGE INTRODUCES, drilled explicitly.

    An httpx.AsyncClient binds its connection pool to the loop that made it. Cache
    one at module level and the second `asyncio.run()` gets a client whose pool
    belongs to a loop that is closed — which surfaces as a connection error, i.e.
    as the service looking down when it is fine. Two real `asyncio.run()` calls,
    the same shim, and both must work.
    """
    from app import knowledge

    seen = []

    async def once(cue: str):
        seen.append(knowledge._client())
        rid = await knowledge.remember("loops", cue, f"loop said: {cue}", good=1)
        hits = await knowledge.recall("loops", cue, k=1)
        return rid, hits

    rid1, hits1 = asyncio.run(once("the first loop wrote this"))
    rid2, hits2 = asyncio.run(once("the second loop wrote this"))

    assert rid1 > 0 and hits1, "the first loop could not reach the store"
    assert rid2 > 0 and hits2, \
        "the second loop reused a client bound to a dead loop — the pool is not " \
        "loop-aware"
    assert seen[0] is not seen[1], \
        "the same AsyncClient was handed to two different event loops"


def test_a_third_loop_still_gets_a_working_client(fresh_db):
    """Not a duplicate of the test above: it proves the per-loop entries do not
    accumulate into something that eventually hands back a stale one. Five loops in
    a row, each of which must reach the service."""
    from app import knowledge

    async def once(n: int) -> int:
        return await knowledge.remember("loops", f"round {n}", f"round {n} ran")

    assert all(asyncio.run(once(n)) > 0 for n in range(5))


async def test_the_pool_closes_this_loops_clients_on_shutdown(fresh_db):
    """main.py's lifespan calls this on the way down. A pooled client holds real
    sockets; a process that exits without closing them leaves peers holding
    half-open connections until their own timeouts reap them."""
    from app import knowledge, pool
    c = knowledge._client()
    assert not c.is_closed
    await pool.aclose_all()
    assert c.is_closed
    # ...and the shim recovers on the next call rather than raising into a
    # degraded path that exists precisely so it cannot raise.
    assert knowledge._client() is not c
    assert await knowledge.remember("a1", "after shutdown", "still writes") > 0


# --- hazard 1, end to end: stop → degraded → start → recovered ---------------

async def test_stop_degraded_start_recovered(fresh_db, monkeypatch):
    """The outage round trip, with the pool in the way.

    A cached client must still fail SOFT when the service goes away (the degraded
    shapes are the contract), and — the part a naive cache breaks — must RECOVER
    when it comes back, with no restart and no reset call from anybody.
    """
    from app import knowledge
    live = httpx.ASGITransport(app=knowledge_service.app)

    monkeypatch.setattr(knowledge, "_TRANSPORT", live)
    assert await knowledge.remember("a1", "before the outage", "it was fine") > 0
    healthy_client = knowledge._client()

    # pc stop knowledge
    monkeypatch.setattr(knowledge, "_TRANSPORT", _DeadTransport())
    assert await knowledge.recall("a1", "before the outage") == [], \
        "a pooled client must still degrade to the empty shape"
    assert await knowledge.remember("a1", "during", "lost") == 0
    down_client = knowledge._client()
    assert down_client is not healthy_client

    # pc start knowledge
    monkeypatch.setattr(knowledge, "_TRANSPORT", live)
    assert await knowledge.remember("a1", "after the outage", "recovered") > 0
    assert await knowledge.recall("a1", "before the outage", k=1) != [], \
        "the shim did not recover when the service came back"
    # ...and having recovered, it is once again ONE client, not one per call.
    assert knowledge._client() is knowledge._client()


def test_the_sync_half_degrades_and_recovers_too(fresh_db, monkeypatch):
    """The same round trip on a sync door — usage, whose whole surface is sync.

    `_client` is rebound here to the pooled accessor usage.py itself declares.
    That is not a shortcut around the thing being tested: conftest replaces the
    sync factories fleet-wide with a TestClient (a sync client cannot drive an ASGI
    app, so the mounted services are reached that way and always have been), which
    means the production sync accessor is unreachable from inside this suite. The
    rebinding restores it for one test, transport seam and all.
    """
    from app import pool, usage

    def _answer(request):
        return httpx.Response(200, json={"rows": [], "total": 0, "window_h": 5,
                                         "budget": 0, "used": 0, "left": 0,
                                         "share": 0.0})

    live = httpx.MockTransport(_answer)
    monkeypatch.setattr(usage, "_client",
                        lambda: pool.sync_client("drill:usage", base_url="http://usage.test",
                                                 timeout=2.0, transport=usage._TRANSPORT))
    monkeypatch.setattr(usage, "_TRANSPORT", live)
    assert usage._client() is usage._client()
    up = usage.snapshot()
    assert up.get("degraded") is not True

    monkeypatch.setattr(usage, "_TRANSPORT", _DeadTransport())
    assert usage.snapshot().get("degraded") is True, \
        "a pooled sync client must fail soft, not raise"

    monkeypatch.setattr(usage, "_TRANSPORT", live)
    assert usage.snapshot().get("degraded") is not True, \
        "the sync shim did not recover when the service came back"


# --- the gateway -------------------------------------------------------------

async def test_the_svc_gateway_shares_one_client():
    """/svc/<name>/* used to build and tear down a client per proxied request —
    the same tax, on the path that serves every service's own UI."""
    from app import pool
    a = pool.async_client("svc-gateway", timeout=30.0)
    b = pool.async_client("svc-gateway", timeout=30.0)
    assert a is b
    await pool.aclose_all()


# --- the lifeworld service's own client, same shape --------------------------

def test_the_lifeworld_substrate_pools_its_ports_too():
    """The chattiest client in the fleet: a scene asks knowledge on every utterance
    and the register on every state change. Same pin as the conductor's shims —
    ports.py is the only door out of that process, so it is the only place that
    may hold a client."""
    src = (REPO / "services" / "lifeworld" / "substrate" / "ports.py").read_text()
    builds = [src[:m.start()].count("\n") + 1
              for m in re.finditer(r"\bhttpx\.(Async)?Client\s*\(", src)]
    # The two inside the pool's own builders, and nothing else.
    pool_lines = [src[:m.start()].count("\n") + 1
                  for m in re.finditer(r"^def _(a?sync)\(", src, re.M)]
    assert len(builds) <= 3, f"ports.py builds clients outside its pool: {builds}"
    assert pool_lines, "ports.py lost its pooled builders"


@pytest.mark.parametrize("door", ["knowledge", "conductor"])
def test_the_substrate_reuses_one_client_per_door(door):
    from conftest import lifeworld_service
    ports = lifeworld_service.ports
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    first = ports._sync(f"drill:{door}", "http://door.test", 2.0, t, {})
    assert ports._sync(f"drill:{door}", "http://door.test", 2.0, t, {}) is first
    other = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    assert ports._sync(f"drill:{door}", "http://door.test", 2.0, other, {}) is not first
