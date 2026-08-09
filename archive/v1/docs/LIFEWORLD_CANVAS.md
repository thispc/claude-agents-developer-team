# The Lifeworld Canvas — a Miro-for-agents you paint

*The plan for turning the Lifeworld from a set of forms and a hardcoded poker table into a
canvas you paint on: a toolbox of agents, artifacts and arrows; draggable figurines with
hitboxes; artifacts with seats that agents snap into; glowing clusters that become one
entity; a manager in the middle; and cloud speech bubbles. From the operator's feedback,
verbatim in spirit: "let user play on canvas like MS Paint / Miro."*

---

## The one decision that unblocks everything: what we build on

The dashboard is deliberately vanilla JS — no build step, no framework, no CDN, and there
are tests that enforce it. A React rewrite (tldraw, React Flow) would mean a build pipeline
and destabilising a working platform. So the call, after surveying the field
([Konva](https://konvajs.org/docs/sandbox/Infinite_Canvas.html), tldraw, Fabric, jsPlumb,
Rete, [infinite-canvas topic](https://github.com/topics/infinite-canvas)):

> **Vendor Konva.js, self-hosted.** A single 175 KB UMD file, zero dependencies, zero
> runtime network calls — served from `dashboard/vendor/konva.min.js` (same-origin, not a
> CDN, no build step). It gives the "big guns": a layered canvas, draggable groups
> (figurines/artifacts), **pixel-perfect hit detection** (the Rocket-League hitbox), zoom
> and pan, and **tweens** for the satisfying magnetic snap. Rich content that canvas is bad
> at — the cloud speech bubbles, portraits — rides in a **DOM overlay** positioned by the
> shared camera transform.

This honours "bring big guns from GitHub" while keeping the platform intact.

---

## The interaction model

### The canvas (a room)
A room is an **infinite canvas with a hidden grid**: pan (drag empty space / space-drag),
zoom (scroll), a faint dotted grid that only shows while dragging (Miro's trick). No room
auto-spawns anything — an empty room is an empty canvas. You add things from a toolbox.

### The toolbox (MS-Paint / Miro)
A dock of tools: **Select** · **Agent** · **Artifact** · **Arrow** · **Manager**. Pick a
tool, click the canvas to place, drag to move. Everything placed is a **token** with a
hitbox.

### Tokens and hitboxes
- An **agent** is a round figurine (its chosen icon/placeholder over a provider-tinted rim,
  reusing the Studio sigil language) with a hitbox = its radius.
- An **artifact** is an object token drawn as its chosen figure (a deck as a small card
  stack, a table as a ringed disc). Hitbox = its bounds.
- **Vicinity** = hitbox overlap within a threshold. When an agent enters an artifact's
  vicinity, **both glow** — the signal that an interaction is possible. (This is the owner's
  "glow indicates interaction has begun.")

### Collating artifacts and seats (the heart)
Some artifacts **collate** — they let agents gather around them (a table, a board). At
creation the operator sets its **number of seats** (like dragging the corners of a square,
or a round table with 3 slots). On canvas the artifact shows its empty seats as sockets
around its rim.

- Drag an agent near a free seat → it is **magnetically pulled into the slot** with a spring
  tween (Konva.Tween), and it locks there. Satisfying by design.
- When agents fill an artifact's seats, the artifact **and** its seated agents share a
  **low glow** — they are now one **cluster** (a single entity).
- Hover the cluster → **"set the script"** and **"add a manager."** A manager token drops in
  the **middle** of the ring, signalling this cluster is being run by one.

### The cluster is the unit of interaction
When anyone in a cluster "talks," they are interacting with everything else in their glow
area (the cluster). A **round** plays out per cluster: the seated agents act in turn,
speaking through **cloud bubbles** near their figurine. (How an utterance is *received* —
the appraisal — is already built; the canvas just drives it per cluster instead of per
room. "How the interaction is received we'll do later" — it's actually done; the engine
does it.)

### Arrows / flows (later)
The Arrow tool draws a directed connection between tokens (Miro-style) — a future channel
for scripted flows between clusters. Planned, not first-wave.

---

## The creation flow (fixing the clunk)

Today: a side drawer with a text box, no feedback, three clicks fire three queued calls.
The fix is a **guided wizard modal**, single-flight, with visible progress:

1. **Tool → place** puts a *pending* token on the canvas immediately (a shimmer placeholder),
   so something happens the instant you act.
2. A **wizard** opens over it: for an agent, "who is this person?" (a brief) with quick
   option-chips *and* a custom box; for an artifact, "what is this thing?" and, if it
   collates, "how many seats?".
3. **Author** — one bounded model call with a **progress bar and a live status** ("dreaming
   up a personality…"). The call is **single-flight**: the button disables and further
   clicks are ignored until it resolves (kills the triple-fire bug).
4. **Choose a face** — the author returns a few **figurine/icon/placeholder options** (and the
   config: dials for a person, seats/look for a thing); the operator picks one or keeps
   custom text. This is the owner's "asks about options of NFTs or icons or placeholder
   figurine, shows full detail of what is happening."
5. **Commit** — the pending token becomes a real, draggable token on the canvas.

Two-step authoring (propose → commit) is what makes step 4 possible: a `/author` endpoint
returns options without creating anything; `/human` or `/artifact` commits the chosen one.

---

## Backend the canvas needs (this wave)

Small, additive extensions to the engine — none break the model:

- **`figure`** on a human and an artifact — the chosen visual token id, set at creation.
- **`slots`** (int) and **`seated`** (list of agent-id-or-null, length = slots) on an
  artifact — a collating artifact and its cluster membership.
- **positions** — entities already carry `pos`; a `POST /{world}/pos` persists a drag.
- **cluster seating** — `POST /{world}/artifact/{id}/seat {slot, human_id}` snaps an agent
  into a slot; unseat frees it. The room view exposes each entity's `pos`/`figure` and each
  collating artifact's `slots`/`seated`, so the canvas can render clusters and glow.
- **cluster-aware round** — a round iterates the room's collating artifacts and plays their
  seated agents (a deck in the cluster deals; neighbours in the ring greet), instead of "all
  agents in the room."
- **two-step authoring** (wave 1.5) — `POST /{world}/author/human|artifact {brief}` returns
  `{options: {dials|slots|look}, faces: [figurine ids]}` for the wizard to present.

---

## Build order

1. **Backend foundation** — figure, slots/seated, pos/move, cluster-seat, cluster round,
   view exposure. (This doc's wave; tested.)
2. **Canvas MVP** — Konva canvas + hidden grid + pan/zoom; toolbox; draggable agent/artifact
   tokens with hitboxes; vicinity glow; **magnetic slot snap**; per-cluster round with cloud
   bubbles. Replaces the themed-room views. **No auto-poker-table.**
3. **The wizard** — guided creation with progress, single-flight, and figurine/option picking
   (two-step authoring).
4. **Manager + set-the-script** — a manager token in the ring; a script the cluster runs.
5. **Arrows / flows** — directed connections between tokens.
6. **Polish** — NFT/icon packs, richer figurines, sound, spring feel.

---

## What "world-class" means here, concretely

Borrowed from the best open-source work, adapted:
- **tldraw / Miro** — the camera (pan/zoom) model, the grid-only-while-dragging, snapping.
- **Rocket League** — proximity hitboxes; a token "wants" the nearest slot and is pulled in.
- **Konva** — the token scene-graph, hit detection, and tween-driven snap that feel physical.
- **Excalidraw** — the calm, hand-made feel over a neutral canvas, not a noisy dashboard.

The bar: placing a token feels instant, dragging feels weighted, a snap feels *earned*, and
a full table quietly glowing tells you — without a word of UI copy — that a society just
formed.
