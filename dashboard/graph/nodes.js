// The Atlas — CARDS: one module as an HTML card, built with createElement and
// textContent ONLY. Everything user- or planner-authored (titles, agent names,
// activity lines) lands via textContent, never markup — a node called
// "<img onerror=…>" renders as its own name.
//
// Two kinds, visually unmistakable (the capillary metaphor):
//   · CHAMBER (a group) — a doorway: layered/stacked shadow implying depth,
//     the child count, rolled-up tests, a big "Enter ▸" affordance. Click = a
//     room transition, never a panel.
//   · CAPILLARY (a leaf) — ONE SERVICE: solid filled card with a distinct accent
//     border, its LIVE STATE chip (running / stopped / idle / unknown — read from
//     process-compose, never guessed), the AGENT front and center (avatar chip +
//     name + ★ mastery), test count, an activity line that pulses while the crew
//     touches it.
// Plus the frame: the AIM card (the room's subject) and the ARTIFACT card (the
// conclusion as the always-last column of the top room).
//
// Every card carries data-key; chips that travel carry data-door. index.js
// resolves both by delegation — no per-card listeners to leak.

export const GLYPH = { aim: "🎯", code: "📦", research: "🔬", data: "🗄",
                       group: "🗂", conclusion: "🏁" };

// Tri-state health — every field OPTIONAL (the payload may predate the backend):
// green = calm glow, yellow = amber pulse, red = urgent slow strobe.
// GREY is the fourth state and it is not "unknown": it means "not started", which
// only a tenant whose cards are WORK can be. A fleet card is never grey (a service
// either beats or it does not), so nothing about the fleet's rendering changes.
const HS_STATES = ["green", "yellow", "red", "grey"];
const HS_TIP = {
  green: "healthy — heartbeat ok, tests green",
  yellow: "tests failing, heartbeat fine",
  red: "heartbeat failing",
  grey: "not started",
};
export function healthClass(n) {
  const s = n && n.health && n.health.status;
  return HS_STATES.includes(s) ? "gr-hs-" + s : "";
}
function syncHealth(el, n) {
  HS_STATES.forEach((s) => el.classList.remove("gr-hs-" + s));
  const c = healthClass(n);
  if (c) el.classList.add(c);
  // `health.note` is the PAYLOAD saying what its own colour means on this card —
  // "delivered", "in flight, a teammate is on it now". Optional, so a payload that
  // sends none (the fleet's) falls back to the tri-state's own words exactly as before.
  const h = (n && n.health) || null;
  const tip = (h && h.note) || (h && HS_TIP[h.status]);
  if (tip) el.title = tip;
}

function elem(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = String(text);   // textContent — never markup
  return el;
}

function testsBrief(tests) {
  const t = tests || {};
  // `tests.brief` is the payload wording its OWN evidence line. A fleet card's
  // evidence is a mapped suite; a project task's is the harness verification the
  // worker ran, and "no tests mapped" would be the wrong sentence for it. Optional,
  // so the fleet (which sends none) keeps the computed line it always had.
  if (t.brief) return String(t.brief);
  if (!t.total) return "no tests mapped";
  // "0/2 passing" reads as "everything is broken" when it actually means nobody has
  // pressed Verify yet. A suite that has never run is not a suite that failed, and the
  // ring already says the same thing by staying grey.
  if (!t.passing && !t.failing) {
    return `${t.total} suite${t.total === 1 ? "" : "s"} · not run yet`;
  }
  return `${t.passing}/${t.total} passing${t.failing ? " · " + t.failing + " failing" : ""}`;
}

function ringClass(tests) {
  const t = tests || {};
  if (!t.total || (!t.passing && !t.failing)) return "gr-ring-none";
  return t.failing ? "gr-ring-bad" : t.passing === t.total ? "gr-ring-good" : "gr-ring-mixed";
}

function agentName(agent) {
  const n = String((agent && agent.name) || "").trim();
  return n || `agent #${(agent && (agent.agent_id ?? agent.home_id)) ?? "?"}`;
}

/** The agent block on a CAPILLARY — front and center, because "who works this"
 * is the first thing a glance must answer. Unassigned says so honestly. */
function agentBlock(n) {
  const row = elem("div", "gr-cagent");
  if (n.agent) {
    const name = agentName(n.agent);
    row.append(elem("span", "gr-cagent-avatar", (name[0] || "?").toUpperCase()),
               elem("b", "gr-cagent-name", name));
    if (n.mastery && n.mastery.master) {
      const star = elem("span", "gr-cagent-star", "★");
      star.title = `master of this service — ${n.mastery.runs} verified runs`;
      row.append(star);
    }
  } else {
    row.classList.add("gr-cagent-none");
    row.append(elem("span", "gr-cagent-avatar", "?"),
               elem("span", "gr-cagent-name", "Unassigned"));
  }
  return row;
}

/** The live STATE chip — the whole point of P6 on one line: is this part of the
 * platform running right now. `unknown` is its own state and says so, because the
 * `--legacy` boot has no fleet manager to ask and pretending "stopped" there
 * would be the card lying about a service that is perfectly up. */
const SVC_TIP = {
  running: "this process is up — right-click, or open the panel, to stop it",
  stopped: "this process is not running — Start brings it back",
  idle: "nothing is running in here right now, which is idle, not broken",
  unknown: "the fleet manager is not answering, so its state cannot be seen",
};
function stateChip(n) {
  const s = n.service || null;
  const state = s && s.state;
  if (!state || state === "none") return null;
  const chip = elem("span", "gr-state gr-state-" + state,
                    (state === "running" ? "● " : state === "unknown" ? "◌ " : "○ ") + state);
  chip.title = SVC_TIP[state] || state;
  return chip;
}

/** Fill (or refill) one card's content. Separate from buildCard so updateCard
 * can repaint data without losing the card's classes or focus state. */
function fill(el, n) {
  while (el.firstChild) el.removeChild(el.firstChild);
  syncHealth(el, n);
  const head = elem("div", "gr-card-head");
  head.append(elem("span", "gr-card-glyph", GLYPH[n.node_type] || GLYPH.code),
              elem("b", "gr-card-title", String(n.title || n.key)));
  const ring = elem("span", "gr-ring " + ringClass(n.tests));
  ring.title = testsBrief(n.tests);
  head.append(ring);
  el.append(head);
  const chip = stateChip(n);
  if (chip) el.append(chip);

  if (n.node_type === "group") {
    const kids = Number(n.children) || 0;
    el.append(elem("div", "gr-card-sub",
                   kids ? `${kids} inside right now` : "nothing inside right now"));
    el.append(elem("div", "gr-card-sub", testsBrief(n.tests)));
    el.append(elem("div", "gr-enter", "Enter ▸"));
  } else if (n.node_type === "aim") {
    if (n.spec) el.append(elem("div", "gr-card-sub gr-card-spec", String(n.spec).slice(0, 140)));
  } else {
    el.append(agentBlock(n));
    el.append(elem("div", "gr-card-sub", testsBrief(n.tests)));
    const act = ((n.activity || [])[0]);
    if (act) el.append(elem("div", "gr-act", "● " + String(act.task || act.what || "working")));
  }
  el.append(elem("div", "gr-doors"));                // door chips land here (index.js)
}

/** One module as a card element. data-key is what click delegation resolves. */
export function buildCard(n) {
  const kind = n.node_type === "group" ? "gr-chamber"
    : n.node_type === "aim" ? "gr-aimcard"
    : "gr-capillary";
  const el = elem("div", "gr-card " + kind);
  el.setAttribute("data-key", String(n.key));
  el.setAttribute("data-kind", n.node_type === "group" ? "chamber" : "capillary");
  el.setAttribute("role", "button");
  fill(el, n);
  return el;
}

/** Repaint an existing card in place (title, ring, agent, health) without
 * touching its focus, busy or reveal state. */
export function updateCard(el, n) {
  const busy = el.classList.contains("gr-busy"), foc = el.classList.contains("gr-focus");
  fill(el, n);
  el.classList.toggle("gr-busy", busy);
  el.classList.toggle("gr-focus", foc);
}

/** The ARTIFACT — the conclusion as the top room's always-last column card.
 * Its live facts (health, beat) ride the payload's `conclusion` dict. */
export function buildArtifactCard(node, conclusion, title) {
  const c = conclusion || {};
  const el = elem("div", "gr-card gr-artifact");
  el.setAttribute("data-key", String((node && node.key) || "conclusion"));
  el.setAttribute("data-kind", "artifact");
  el.setAttribute("role", "button");
  const head = elem("div", "gr-card-head");
  head.append(elem("span", "gr-card-glyph", GLYPH.conclusion),
              elem("b", "gr-card-title", String(title || "The Artifact")));
  const dot = elem("span", "gr-goal-dot gr-goaldot-" + String(c.health || "unknown"));
  dot.title = "health: " + String(c.health || "unknown");
  head.append(dot);
  el.append(head);
  el.append(elem("div", "gr-card-sub", "the GOAL everything feeds"));
  if (c.fleet) {
    el.append(elem("div", "gr-card-sub", c.fleet.visible
      ? `fleet: ${c.fleet.running}/${c.fleet.declared} up`
      : "fleet manager not answering"));
  }
  if (c.beat != null) el.append(elem("div", "gr-card-sub", "beat: " + String(c.beat)));
  el.append(elem("div", "gr-doors"));
  return el;
}
