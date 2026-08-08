"""pool.py — one httpx client per service door, kept open.

WHY THIS EXISTS. Every service shim used to build a brand new client per call
(`async with _client() as c:`). Measured on the live fleet: a fresh client costs
5.6ms per localhost call against 0.56ms on a pooled connection — a 10x tax, and
none of it is the service's work. It is TCP setup plus httpx's own pool/transport
construction, paid again on every hop. `/api/graph/self` (the Atlas) makes ~43
service calls and took 219ms; almost all of that was this.

So the shims share a client instead of minting one. The old comment in knowledge.py
("a client is built per call — at the platform's call rates the ~0.1ms construction
cost buys freedom from event-loop lifetime bugs") was right about the hazard and
wrong about the price by a factor of fifty. This module pays the hazard down
explicitly rather than by throwing the connection away.

THE THREE HAZARDS, AND WHAT IS DONE ABOUT EACH

1. A CLIENT OUTLIVING ITS CONFIGURATION. Tests point a shim at a mounted service by
   swapping `_TRANSPORT` (an ASGITransport onto the real app) or `_URL`, and drill
   outages by swapping in a transport that refuses. A client cached under a bare
   name would keep the transport it was born with and every one of those drills
   would silently stop testing anything.

   So the cache holds an IDENTITY next to the client — (base_url, transport,
   timeout, headers) — and rebuilds the moment any of it differs. This is a keyed
   cache rather than an explicit `reset()` hook that conftest calls, deliberately:
   a reset hook is correct only as long as every present and future test remembers
   to call it, and a test that forgets does not fail, it passes against the wrong
   service. Invalidation by identity cannot be forgotten. (A shim whose sync half
   has no transport seam at all — knowledge, notify — is still swapped the way it
   always was, by monkeypatching the accessor itself; that path is untouched.)

2. AN ASYNC CLIENT OUTLIVING ITS EVENT LOOP. `httpx.AsyncClient` binds its
   connection pool to the loop that created it; reused from another loop it fails
   in ways that look like the network. The conductor runs exactly one loop, but
   pytest-asyncio builds a loop per test and any `asyncio.run()` builds another —
   so a naive module-level client is a bug that only shows up in the suite (or in
   any tool that calls a shim twice).

   The async cache is therefore keyed BY THE RUNNING LOOP, in a WeakKeyDictionary:
   a new loop gets a new client, and when a loop is collected its clients go with
   it. A client from a dead loop can never be handed out, because a dead loop is
   never the running loop. Its sockets are closed by the garbage collector rather
   than by `aclose()` — there is no loop left to await on — which is why the
   entries are weak: nothing accumulates.

3. THREADS. The sync half is shared across the log flush daemon and the request
   threads. `httpx.Client` is thread-safe for sending; only the build is guarded,
   with a lock, so two threads racing a cold cache cannot leak a client.

Everything here is best-effort and never raises: a shim's job is to degrade, and a
pool that could throw on the way to a degraded path would defeat it.
"""

from __future__ import annotations

import asyncio
import threading
import weakref

import httpx

# name -> (identity, client). One sync client per door.
_SYNC: dict[str, tuple[tuple, httpx.Client]] = {}
# loop -> {name: (identity, client)}. Weak, so a finished loop takes its clients
# with it — see hazard 2.
_ASYNC: "weakref.WeakKeyDictionary[object, dict[str, tuple[tuple, httpx.AsyncClient]]]" \
    = weakref.WeakKeyDictionary()

_LOCK = threading.Lock()

# How many clients this module has actually constructed, per door. Not a metric —
# a TEST SEAM. "the shim must not build a client per call" is a claim about
# construction, and counting it here is the only way to assert it that does not
# depend on how httpx spells its internals.
CONSTRUCTED: dict[str, int] = {}


def _identity(base_url: str, timeout: float, transport, headers: dict | None) -> tuple:
    """Everything that would make a differently-configured client. Compared with
    `==`, which for a transport object falls back to identity — exactly what a
    monkeypatched ASGITransport/MockTransport needs."""
    return (str(base_url or ""), float(timeout or 0), transport,
            tuple(sorted((headers or {}).items())))


def sync_client(name: str, *, base_url: str = "", timeout: float = 10.0,
                transport: httpx.BaseTransport | None = None,
                headers: dict | None = None) -> httpx.Client:
    """The shared sync client for `name`, built on first use.

    Rebuilt when the configuration changed (a test swapped the transport, the token
    was re-read) or when the previous one was closed — a shim that keeps working
    after shutdown closed its pool is worth more than a shim that raises.
    """
    ident = _identity(base_url, timeout, transport, headers)
    hit = _SYNC.get(name)
    if hit is not None and hit[0] == ident and not hit[1].is_closed:
        return hit[1]
    with _LOCK:
        hit = _SYNC.get(name)
        if hit is not None and hit[0] == ident and not hit[1].is_closed:
            return hit[1]
        if hit is not None:
            _close_quietly(hit[1])
        c = httpx.Client(base_url=base_url, timeout=timeout, transport=transport,
                         headers=headers or {})
        _SYNC[name] = (ident, c)
        CONSTRUCTED[name] = CONSTRUCTED.get(name, 0) + 1
        return c


def async_client(name: str, *, base_url: str = "", timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None,
                 headers: dict | None = None) -> httpx.AsyncClient:
    """The shared async client for `name` ON THIS EVENT LOOP, built on first use.

    Must be called from inside a running loop — every caller is an `async def`, and
    binding a client to a loop that is not running is the bug this guards against.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop at all: hand back an unshared client rather than cache one under
        # a key that does not exist. Callers are all async, so this is a
        # belt-and-braces path (a tool driving a shim's coroutine by hand).
        CONSTRUCTED[name] = CONSTRUCTED.get(name, 0) + 1
        return httpx.AsyncClient(base_url=base_url, timeout=timeout,
                                 transport=transport, headers=headers or {})
    ident = _identity(base_url, timeout, transport, headers)
    with _LOCK:
        by_name = _ASYNC.get(loop)
        if by_name is None:
            by_name = {}
            _ASYNC[loop] = by_name
        hit = by_name.get(name)
        if hit is not None and hit[0] == ident and not hit[1].is_closed:
            return hit[1]
        c = httpx.AsyncClient(base_url=base_url, timeout=timeout,
                              transport=transport, headers=headers or {})
        by_name[name] = (ident, c)
        CONSTRUCTED[name] = CONSTRUCTED.get(name, 0) + 1
        return c


# --- shutdown ----------------------------------------------------------------

def _close_quietly(c) -> None:
    try:
        c.close()
    except Exception:
        pass


def close_sync() -> None:
    """Close every sync client. Safe to call twice; a later call rebuilds."""
    with _LOCK:
        for _ident, c in _SYNC.values():
            _close_quietly(c)
        _SYNC.clear()


async def aclose_async() -> None:
    """Close the async clients belonging to the RUNNING loop — the only ones this
    loop is allowed to await on. Clients from other loops are not ours to close;
    they are freed with their loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    with _LOCK:
        by_name = _ASYNC.pop(loop, {}) or {}
    for _ident, c in by_name.values():
        try:
            await c.aclose()
        except Exception:
            pass


async def aclose_all() -> None:
    """Everything, on the way down: this loop's async clients and all sync ones."""
    await aclose_async()
    close_sync()
