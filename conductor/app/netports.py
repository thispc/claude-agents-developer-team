"""Port reservation for everything that binds on 127.0.0.1 — one allocator, shared.

Moved verbatim from deploy._free_port, which was the good one. The other pattern in
this codebase (sandbox's connect_ex probe) told you a port WAS free a moment ago,
not that it still is — and its 8700+ scan overlapped deploy's 8600-8799 range, so a
sandbox and a deployed app could collide. Both now come here, on disjoint ranges
(see services.yaml: sandbox 8500-8599, apps 8600-8799).
"""

import socket


def reserve_port(start: int, count: int, skip: set[int] | frozenset = frozenset(),
                 label: str = "a port") -> tuple[int, socket.socket]:
    """Reserve a free port in [start, start+count), returning it still bound.

    connect_ex-then-close tells you a port was free a moment ago, not that it
    still is: the gap before the child actually binds it can be as long as the
    build (docker build, npm install, ...), so two deploys started together can
    scan, both see the same port free, and both be handed it. Binding it here
    with SO_REUSEADDR and handing back the open socket makes the OS the referee
    — a concurrent call's bind() on that port fails outright — and the caller
    holds the reservation until moments before the child starts.
    """
    for p in range(start, start + count):
        if p in skip:
            continue
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", p))
        except OSError:
            s.close()
            continue
        return p, s
    raise RuntimeError(f"no free port available for {label}")
