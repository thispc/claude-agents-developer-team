# layout-graph

**Not hashed.** Reword freely.

Places nodes in layers and routes the edges between them.

## Why this is a module

Because it is a pure function of the graph, and therefore exactly the kind of
thing that should be one: the same system always draws the same picture, which
the determinism gate can check and a person can rely on. Layout hidden inside a
renderer would be untestable and would drift.

## The rules

- **Layers by longest path.** A node sits one layer to the right of the furthest
  node that feeds it. That puts sources on the left and sinks on the right.
- **Cycles do not hang it.** Relaxation runs a bounded number of times, so a
  cycle simply stops moving nodes rightward. The edge that points backwards is
  marked `back: true` and drawn differently — a cycle you can see beats a cycle
  the layout quietly pretends is not there.
- **Order within a layer is input order.** Not alphabetical, not by degree —
  input order, because it is the only rule that is stable, explainable, and under
  the author's control.
- **The module emits the SVG path itself.** Nothing downstream has to know
  geometry, and the curve can change without touching the renderer.

## Shared state
None.
