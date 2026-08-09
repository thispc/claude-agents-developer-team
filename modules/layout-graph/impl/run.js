const BOX = { w: 200, h: 96, gapX: 96, gapY: 28 };
const PAD = 24;

/** @param {any} input */
export function layout(input) {
  if (input === null || typeof input !== "object" || Array.isArray(input)) throw bad("input must be an object");
  const nodes = input.nodes;
  const edges = input.edges;
  if (!Array.isArray(nodes)) throw bad("nodes must be an array");
  if (!Array.isArray(edges)) throw bad("edges must be an array");

  const box = { ...BOX, ...(input.box ?? {}) };
  const ids = nodes.map((/** @type {any} */ n, /** @type {number} */ i) => {
    if (n === null || typeof n !== "object" || typeof n.id !== "string" || n.id === "") throw bad(`node ${i} has no id`);
    return n.id;
  });
  const known = new Set(ids);
  if (known.size !== ids.length) throw bad("two nodes share an id");
  for (const [i, e] of edges.entries()) {
    if (e === null || typeof e !== "object") throw bad(`edge ${i} is not an object`);
    if (!known.has(e.from)) throw bad(`edge ${i} comes from "${e.from}", which is not a node here`);
    if (!known.has(e.to)) throw bad(`edge ${i} goes to "${e.to}", which is not a node here`);
  }

  // ── find the edges that close a cycle, and lay out everything else.
  //
  // Relaxing over ALL edges would not merely be slow on a cyclic graph, it would
  // be wrong: each trip round the loop pushes both ends one layer further right
  // for ever, so the layering ends up meaningless and every edge looks backward.
  // A depth-first walk names the closing edge exactly — the one that reaches a
  // node still on the current stack — and layering then runs on what is left,
  // which is a DAG and therefore terminates on its own.
  const outgoing = new Map(ids.map((id) => [id, /** @type {{to: string, i: number}[]} */ ([])]));
  edges.forEach((/** @type {any} */ e, /** @type {number} */ i) => outgoing.get(e.from)?.push({ to: e.to, i }));

  /** @type {Set<number>} */
  const closing = new Set();
  const onStack = new Set();
  const finished = new Set();
  /** @param {string} u */
  const walk = (u) => {
    onStack.add(u);
    for (const { to, i } of outgoing.get(u) ?? []) {
      if (onStack.has(to)) closing.add(i);
      else if (!finished.has(to)) walk(to);
    }
    onStack.delete(u);
    finished.add(u);
  };
  // Started in input order, so which edge of a cycle is called "the closing one"
  // is the author's choice rather than an accident of hashing.
  for (const id of ids) if (!finished.has(id)) walk(id);

  /** @type {Record<string, number>} */
  const layer = {};
  for (const id of ids) layer[id] = 0;
  for (let pass = 0; pass < ids.length; pass++) {
    let moved = false;
    edges.forEach((/** @type {any} */ e, /** @type {number} */ i) => {
      if (closing.has(i)) return;
      const want = /** @type {number} */ (layer[e.from]) + 1;
      if (want > /** @type {number} */ (layer[e.to])) { layer[e.to] = want; moved = true; }
    });
    if (!moved) break;
  }

  // Order within a layer is INPUT ORDER — the only rule that is stable,
  // explainable, and under the author's control. Alphabetical would reshuffle
  // the picture every time a module is renamed.
  /** @type {Record<number, string[]>} */
  const column = {};
  for (const id of ids) {
    const l = /** @type {number} */ (layer[id]);
    (column[l] ??= []).push(id);
  }

  const layers = Math.max(...ids.map((id) => /** @type {number} */ (layer[id]) + 1), 0);
  const tallest = Math.max(...Object.values(column).map((c) => c.length), 0);
  const height = PAD * 2 + tallest * box.h + Math.max(0, tallest - 1) * box.gapY;
  const width = PAD * 2 + layers * box.w + Math.max(0, layers - 1) * box.gapX;

  /** @type {Record<string, {x: number, y: number}>} */
  const at = {};
  /** @type {any[]} */
  const placed = [];
  for (const id of ids) {
    const l = /** @type {number} */ (layer[id]);
    const col = /** @type {string[]} */ (column[l]);
    const row = col.indexOf(id);
    const colHeight = col.length * box.h + (col.length - 1) * box.gapY;
    const x = PAD + l * (box.w + box.gapX);
    // Columns are centred against each other, so a one-node column sits beside
    // the middle of a three-node column rather than at its top.
    const y = Math.round((height - colHeight) / 2) + row * (box.h + box.gapY);
    at[id] = { x, y };
    placed.push({ id, x, y, w: box.w, h: box.h, layer: l });
  }

  const routed = edges.map((/** @type {any} */ e, /** @type {number} */ i) => {
    const a = /** @type {{x: number, y: number}} */ (at[e.from]);
    const b = /** @type {{x: number, y: number}} */ (at[e.to]);
    const back = closing.has(i);
    return { from: e.from, to: e.to, back, path: curve(a, b, box, back) };
  });

  return { width, height, layers, nodes: placed, edges: routed };
}

/**
 * A horizontal cubic between the right edge of one box and the left edge of the
 * next. Backward edges leave and re-enter on the same side and bow outward, so a
 * cycle reads as a cycle instead of a line crossing the picture.
 * @param {{x: number, y: number}} a @param {{x: number, y: number}} b
 * @param {{w: number, h: number, gapX: number}} box @param {boolean} back
 */
function curve(a, b, box, back) {
  const half = Math.round(box.h / 2);
  if (back) {
    const x1 = a.x, y1 = a.y + half;
    const x2 = b.x + box.w, y2 = b.y + half;
    const lift = Math.round(box.gapX * 0.9);
    return `M ${x1} ${y1} C ${x1 - lift} ${y1 - lift} ${x2 + lift} ${y2 - lift} ${x2} ${y2}`;
  }
  const x1 = a.x + box.w, y1 = a.y + half;
  const x2 = b.x, y2 = b.y + half;
  const bend = Math.max(24, Math.round((x2 - x1) / 2));
  return `M ${x1} ${y1} C ${x1 + bend} ${y1} ${x2 - bend} ${y2} ${x2} ${y2}`;
}

/** @param {string} message */
function bad(message) {
  return Object.assign(new Error(message), { code: "EBADGRAPH" });
}
