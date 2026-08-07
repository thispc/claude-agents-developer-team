// Module graph — LAYOUT: a layered (Sugiyama-lite) arrangement computed from the
// edges alone, deterministically and without a library. Depth = the LONGEST path
// from the aim node, so a module never sits left of something it depends on;
// within a layer, nodes settle near the average slot of their parents
// (barycenter), which is what stops the wires from weaving. Saved positions —
// someone dragged that node — always win over anything computed here.

const COL_W = 260;   // x distance between layers (default; a level can ask for more air)
const ROW_H = 150;   // y distance between slots in a layer

/** src/dst regardless of which serializer produced the edge (the seam's shapes
 * use src/dst; the store's raw rows use src_key/dst_key). */
function ends(e) {
  return [String(e.src ?? e.src_key ?? ""), String(e.dst ?? e.dst_key ?? "")];
}

/** {key -> {x, y}} for every node. `positions` is the persisted {key: [x,y]} map;
 * a key present there is used verbatim and skipped by the auto-layout. `opts` may
 * widen the grid ({colW, rowH}) — the top level of the hierarchical view breathes
 * like an architecture diagram, a group's interior packs a little tighter. */
export function layout(nodes, edges, positions, opts) {
  positions = positions || {};
  const colW = (opts && opts.colW) || COL_W;
  const rowH = (opts && opts.rowH) || ROW_H;
  const keys = nodes.map((n) => String(n.key));
  const have = new Set(keys);
  const preds = new Map(keys.map((k) => [k, []]));
  const succs = new Map(keys.map((k) => [k, []]));
  let m = 0;
  for (const e of edges || []) {
    const [s, d] = ends(e);
    if (!have.has(s) || !have.has(d) || s === d) continue;
    succs.get(s).push(d); preds.get(d).push(s); m++;
  }

  // Longest-path depth via Kahn's topological order. The aim node (or any node
  // nothing points at) is depth 0. If a cycle sneaks in, whatever cannot be
  // ordered lands one column past the deepest ordered node instead of hanging.
  const indeg = new Map(keys.map((k) => [k, preds.get(k).length]));
  const depth = new Map(keys.map((k) => [k, 0]));
  const queue = keys.filter((k) => !indeg.get(k));
  const ordered = [];
  while (queue.length) {
    const k = queue.shift();
    ordered.push(k);
    for (const d of succs.get(k)) {
      depth.set(d, Math.max(depth.get(d), depth.get(k) + 1));
      indeg.set(d, indeg.get(d) - 1);
      if (!indeg.get(d)) queue.push(d);
    }
  }
  if (ordered.length < keys.length) {
    const deepest = Math.max(0, ...ordered.map((k) => depth.get(k)));
    for (const k of keys) if (!ordered.includes(k)) depth.set(k, deepest + 1);
  }
  // The conclusion sits alone past everything, even when only some modules feed it.
  const conc = nodes.filter((n) => n.node_type === "conclusion").map((n) => String(n.key));
  const deepestNonConc = Math.max(0, ...keys.filter((k) => !conc.includes(k)).map((k) => depth.get(k)));
  for (const k of conc) depth.set(k, Math.max(depth.get(k), deepestNonConc + (m ? 1 : 0)));

  // Layers, then barycenter: order each layer by the average slot of its parents
  // in the previous pass (parents with no slot yet contribute nothing).
  const layers = new Map();
  for (const k of keys) {
    const d = depth.get(k);
    if (!layers.has(d)) layers.set(d, []);
    layers.get(d).push(k);
  }
  const slot = new Map();
  const depths = [...layers.keys()].sort((a, b) => a - b);
  for (const d of depths) {
    const layer = layers.get(d);
    const bary = (k) => {
      const ps = preds.get(k).filter((p) => slot.has(p));
      if (!ps.length) return layer.indexOf(k);           // keep authored order
      return ps.reduce((s, p) => s + slot.get(p), 0) / ps.length;
    };
    layer.sort((a, b) => bary(a) - bary(b) || layer.indexOf(a) - layer.indexOf(b));
    layer.forEach((k, i) => slot.set(k, i));
  }

  const out = {};
  for (const d of depths) {
    const layer = layers.get(d);
    layer.forEach((k, i) => {
      const saved = positions[k];
      out[k] = (Array.isArray(saved) && saved.length === 2)
        ? { x: +saved[0], y: +saved[1] }
        : { x: d * colW, y: (i - (layer.length - 1) / 2) * rowH };
    });
  }
  return out;
}

/** The reveal order: by depth, then slot — the story reads aim → modules → conclusion. */
export function topoOrder(nodes, edges) {
  const pos = layout(nodes, edges, {});   // computed only — saved drags must not reorder the story
  return nodes.map((n) => String(n.key))
    .sort((a, b) => (pos[a].x - pos[b].x) || (pos[a].y - pos[b].y));
}
