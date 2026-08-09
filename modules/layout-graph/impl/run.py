# A Python implementation of the layout-graph contract.
#
# Written from interface.json, conformance.json and behavior.md. Where those are
# silent I chose for myself rather than guessing at what the first implementation
# did — which is the entire point of a second implementation. If the two disagree
# somewhere, the contract failed to say something it needed to say.
#
# What the pinned cases let me DERIVE, rather than guess:
#   the override case gives 2*PAD + 100 == 148, so PAD == 24
#   two stacked nodes at y=24 and y=148 give h + gapY == 124
#   three stacked giving height 392 gives 3h + 2*gapY == 344, so h == 96, gapY == 28
#   the path "M 224 72 …" gives 24 + w == 224 so w == 200, and 224 + gapX == 320
#     so gapX == 96, and 24 + h/2 == 72 confirms h == 96
#
# What the contract does NOT say, where I made my own call:
#   * how much a cubic's control points are offset. The pinned case fixes ONE
#     number (48) and a dozen rules produce 48 there. I key the offset to the
#     configured gap, because that is the parameter the caller actually set.
#   * what a backward edge's path looks like at all. No case pins it.
#   * how to round when centring lands on a half pixel. No case is odd.
#   * what a self-loop should do.

PAD = 24
BOX = {"w": 200, "h": 96, "gapX": 96, "gapY": 28}


class _Refusal(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.code = "EBADGRAPH"


def layout(payload):
    if not isinstance(payload, dict):
        raise _Refusal("input must be an object")
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, list):
        raise _Refusal("nodes must be an array")
    if not isinstance(edges, list):
        raise _Refusal("edges must be an array")

    box = dict(BOX)
    override = payload.get("box")
    if isinstance(override, dict):
        box.update(override)

    ids = []
    for i, n in enumerate(nodes):
        if not isinstance(n, dict) or not isinstance(n.get("id"), str) or n["id"] == "":
            raise _Refusal(f"node {i} has no id")
        ids.append(n["id"])
    if len(set(ids)) != len(ids):
        raise _Refusal("two nodes share an id")

    known = set(ids)
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            raise _Refusal(f"edge {i} is not an object")
        if e.get("from") not in known:
            raise _Refusal(f'edge {i} comes from "{e.get("from")}", which is not a node here')
        if e.get("to") not in known:
            raise _Refusal(f'edge {i} goes to "{e.get("to")}", which is not a node here')

    closing = _closing_edges(ids, edges)
    layer = _layers(ids, edges, closing)

    # Order within a layer is input order — the contract is explicit about this.
    column = {}
    for node_id in ids:
        column.setdefault(layer[node_id], []).append(node_id)

    layers = max((layer[i] + 1 for i in ids), default=0)
    tallest = max((len(c) for c in column.values()), default=0)
    height = PAD * 2 + tallest * box["h"] + max(0, tallest - 1) * box["gapY"]
    width = PAD * 2 + layers * box["w"] + max(0, layers - 1) * box["gapX"]

    at = {}
    placed = []
    for node_id in ids:
        lay = layer[node_id]
        col = column[lay]
        row = col.index(node_id)
        col_height = len(col) * box["h"] + (len(col) - 1) * box["gapY"]
        x = PAD + lay * (box["w"] + box["gapX"])
        # CONTRACT IS SILENT on half-pixel centring. Integer division is the
        # obvious thing to write in Python and every pinned case is even.
        y = (height - col_height) // 2 + row * (box["h"] + box["gapY"])
        at[node_id] = (x, y)
        placed.append({"id": node_id, "x": x, "y": y, "w": box["w"], "h": box["h"], "layer": lay})

    routed = []
    for i, e in enumerate(edges):
        back = i in closing
        routed.append({
            "from": e["from"], "to": e["to"], "back": back,
            "path": _curve(at[e["from"]], at[e["to"]], box, back),
        })

    return {"width": width, "height": height, "layers": layers, "nodes": placed, "edges": routed}


def _closing_edges(ids, edges):
    """Which edges close a cycle.

    behavior.md describes bounded relaxation over every edge. I could not make
    that satisfy the conformance case: relaxing across a two-node cycle pushes
    both ends rightward on every pass, so after |nodes| passes the layering is
    meaningless and BOTH edges look backward — while the case requires a->b to be
    back:false and b->a to be back:true. A depth-first walk names the closing
    edge exactly, and is the only reading I found that passes.
    """
    outgoing = {i: [] for i in ids}
    for i, e in enumerate(edges):
        outgoing[e["from"]].append((e["to"], i))

    closing, on_stack, finished = set(), set(), set()

    def walk(u):
        on_stack.add(u)
        for to, i in outgoing[u]:
            if to in on_stack:
                closing.add(i)
            elif to not in finished:
                walk(to)
        on_stack.discard(u)
        finished.add(u)

    for node_id in ids:
        if node_id not in finished:
            walk(node_id)
    return closing


def _layers(ids, edges, closing):
    layer = {i: 0 for i in ids}
    for _ in range(len(ids)):
        moved = False
        for i, e in enumerate(edges):
            if i in closing:
                continue
            want = layer[e["from"]] + 1
            if want > layer[e["to"]]:
                layer[e["to"]] = want
                moved = True
        if not moved:
            break
    return layer


def _curve(a, b, box, back):
    """CONTRACT IS SILENT on the shape of the curve.

    The one pinned path fixes a single control offset of 48, and many rules give
    48 there. I key the offset to the configured horizontal gap, since that is
    the parameter the caller actually set — a wider gap should bow more, and an
    edge spanning three layers should not bow three times as hard.
    """
    half = box["h"] // 2
    bend = box["gapX"] // 2
    if back:
        # Nothing pins this at all. Leaving and re-entering on the same side, so
        # a backward edge reads as one rather than crossing the picture.
        x1, y1 = a[0], a[1] + half
        x2, y2 = b[0] + box["w"], b[1] + half
        return f"M {x1} {y1} C {x1 - bend} {y1 - bend} {x2 + bend} {y2 - bend} {x2} {y2}"
    x1, y1 = a[0] + box["w"], a[1] + half
    x2, y2 = b[0], b[1] + half
    return f"M {x1} {y1} C {x1 + bend} {y1} {x2 - bend} {y2} {x2} {y2}"
