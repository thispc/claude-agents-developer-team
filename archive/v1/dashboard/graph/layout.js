// The Atlas — COLUMNS: one room's dependency order, computed from the room's
// own edges alone, deterministically and without a library. A card's column is
// the LONGEST path from a source (something no in-room edge points at), so a
// card never sits left of something that feeds it; within a column, cards
// settle near the average slot of their feeders (barycenter), which is what
// stops the door wires from weaving. There are NO PIXELS here — the room is a
// CSS grid and the grid owns all geometry; this module only answers "which
// column, in what order". A cycle cannot hang it: whatever Kahn's order cannot
// reach lands one column past the deepest ordered card.

/** src/dst regardless of which serializer produced the edge (the seam's shapes
 * use src/dst; the store's raw rows use src_key/dst_key). */
function ends(e) {
  return [String(e.src ?? e.src_key ?? ""), String(e.dst ?? e.dst_key ?? "")];
}

/** nodes + in-room edges → an array of COLUMNS, each an ordered array of node
 * keys. Deterministic: same payload, same columns, every time. */
export function columns(nodes, edges) {
  const keys = nodes.map((n) => String(n.key));
  const have = new Set(keys);
  const preds = new Map(keys.map((k) => [k, []]));
  const succs = new Map(keys.map((k) => [k, []]));
  for (const e of edges || []) {
    const [s, d] = ends(e);
    if (!have.has(s) || !have.has(d) || s === d) continue;
    succs.get(s).push(d);
    preds.get(d).push(s);
  }

  // Longest-path depth via Kahn's topological order.
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

  // Columns, then barycenter: order each column by the average slot of its
  // feeders in the previous pass (feeders with no slot yet contribute nothing;
  // ties keep authored order).
  const byDepth = new Map();
  for (const k of keys) {
    const d = depth.get(k);
    if (!byDepth.has(d)) byDepth.set(d, []);
    byDepth.get(d).push(k);
  }
  const slot = new Map();
  const out = [];
  for (const d of [...byDepth.keys()].sort((a, b) => a - b)) {
    const col = byDepth.get(d);
    const bary = (k) => {
      const ps = preds.get(k).filter((p) => slot.has(p));
      if (!ps.length) return col.indexOf(k);
      return ps.reduce((s, p) => s + slot.get(p), 0) / ps.length;
    };
    col.sort((a, b) => bary(a) - bary(b) || col.indexOf(a) - col.indexOf(b));
    col.forEach((k, i) => slot.set(k, i));
    out.push(col);
  }
  return out;
}

/** The stagger order: column by column, top to bottom — the room's story reads
 * left to right, feeders before fed. */
export function topoOrder(nodes, edges) {
  return columns(nodes, edges).flat();
}
