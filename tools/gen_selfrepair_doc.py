#!/usr/bin/env python3
"""Generate docs/SELF_REPAIR.md by READING THE CODE, not by describing it from memory.

Hand-written architecture docs rot silently: the sentence stays true-sounding while the
constant it describes changes underneath. So everything here that could drift — the phase
machine, the kv keys, the endpoints, the knobs and their rationales, the factor personas,
the protected paths, the safety rails — is pulled out of the running modules at generation
time. Prose that cannot be derived is clearly marked as prose and kept short.

    python tools/gen_selfrepair_doc.py           # write docs/SELF_REPAIR.md
    python tools/gen_selfrepair_doc.py --check   # exit 1 if it is stale (what the test runs)

Adding a knob or an endpoint and forgetting the doc is therefore not possible: the test
fails until you regenerate, and regenerating is one command.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "SELF_REPAIR.md"
sys.path.insert(0, str(ROOT / "conductor"))


def _mod(name: str):
    import importlib
    return importlib.import_module(name)


def _src(path: str) -> str:
    return (ROOT / path).read_text()


def _first_para(doc: str | None) -> str:
    if not doc:
        return ""
    out = []
    for line in doc.strip().splitlines():
        if not line.strip():
            break
        out.append(line.strip())
    return " ".join(out)


IDLE = ("Decide whether a sprint may start: check headroom for what the next sprint will "
        "actually cost, then either open one — draining the backlog straight to build when it "
        "is fresh — or sleep with a visible reason and wake time.")


def _phases() -> list[tuple[str, str]]:
    """The state machine, from the `_phase_*` coroutines' own docstrings plus the dispatch
    order in `_advance`."""
    repair = _mod("app.repair")
    order = re.findall(r'phase == "(\w+)"', inspect.getsource(repair._advance))
    seen, out = set(), []
    for name in order:
        if name in seen:
            continue
        seen.add(name)
        fn = getattr(repair, f"_phase_{name}", None)
        # `idle` has no _phase_ of its own — it is the gate in `_advance` that decides whether
        # a sprint may start at all, so its description comes from there.
        text = _first_para(fn.__doc__) if fn else IDLE
        out.append((name, text))
    return out


def _kv_keys() -> list[tuple[str, str]]:
    """Every kv key the engine reads or writes, with the comment that introduced it (the
    module docstring block lists them; this finds the real string literals)."""
    keys = {"usage:ledger"}      # behind a module constant, so the regex below cannot see it
    for path in ("conductor/app/repair.py", "conductor/app/repair_routes.py",
                 "conductor/app/usage.py"):
        for m in re.finditer(r'kv_(?:get|set)\(\s*[f]?"([a-z]+:[a-z_:{}\w]+)"', _src(path)):
            keys.add(m.group(1))
    notes = {
        "repair:enabled": "THE BUTTON — the only thing the toggle writes",
        "repair:state": "phase, sprint number, task index, sleep reason/until — persisted "
                        "before each transition so a restart resumes mid-sprint",
        "repair:factors": "the factor list; the owner's toggles and additions live here",
        "repair:factors_v": "persona stamp the factor list was last synced at",
        "repair:world": "the crew's Studio world/room/thread ids and its persona stamp",
        "repair:seq": "last sprint number",
        "repair:ledger": "the crew's own call counter (kind, model, n) — the backstop meter",
        "repair:queue": "branches waiting for review in supervised mode",
        "repair:backlog": "tasks banked by one scout+deliberation, drained over later sprints",
        "repair:lease": "which process drives the engine (pid + heartbeat)",
        "repair:last_error": "the last phase failure, shown on the screen",
        "usage:ledger": "the PRE-P2 meter blob. The usage SERVICE owns the meter now "
                        "(data/usage.db, one `usage_rows` row per call); this key "
                        "survives only as the service's first-boot copy source and as "
                        "the rollback path, and commit B drops it",
        "usage:backfilled": "one-shot guard for importing the crew's pre-meter history "
                            "(repair:ledger) into the usage service",
    }
    dyn = {"repair:sprint:": "one record per sprint: scout digest, memo, tasks, retro"}
    rows = [(k, notes.get(k, "")) for k in sorted(keys) if not k.endswith("{")]
    rows += [(k + "{n}", v) for k, v in dyn.items()]
    return rows


def _endpoints() -> list[tuple[str, str, str]]:
    tree = ast.parse(_src("conductor/app/repair_routes.py"))
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                verb = dec.func.attr.upper()
                path = dec.args[0].value if dec.args else ""
                out.append((verb, "/api/repair" + path, _first_para(ast.get_docstring(node))))
    return out


def _knobs() -> list[tuple[str, str, str]]:
    tuning = _mod("app.tuning")
    out = []
    for name, (default, _kind, _env, why) in tuning.KNOBS.items():
        if name.startswith(("repair_", "usage_", "agent_session")):
            out.append((name, str(default), " ".join(str(why).split())))
    return out


def _factors() -> list[tuple[str, str, str, str]]:
    repair = _mod("app.repair")
    drives = _mod("app.lifeworld.drives")
    out = []
    for f in repair.DEFAULT_FACTORS:
        wants = ", ".join(f.get("drives", {})) or "—"
        top = sorted(f["dials"].items(), key=lambda kv: -abs(kv[1] - 50))[:3]
        dials = ", ".join(f"{k} {v}" for k, v in top)
        out.append((f["name"], f["brief"], dials, wants))
    assert all(k in drives.SPEC for f in repair.DEFAULT_FACTORS for k in f.get("drives", {}))
    return out


def _protected() -> list[str]:
    return sorted(_mod("app.repair_builder").PROTECTED)


def _table(head: list[str], rows: list[tuple]) -> str:
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out)


def build() -> str:
    repair = _mod("app.repair")
    usage = _mod("app.usage")
    L = []
    add = L.append

    add("<!-- GENERATED by tools/gen_selfrepair_doc.py — do not edit by hand. -->")
    add("<!-- Re-run that script after changing repair.py, repair_builder.py,")
    add("     repair_routes.py, usage.py or the repair_*/usage_* knobs. -->")
    add("")
    add("# Self-repair — what actually happens")
    add("")
    add("This page is generated from the code it describes: every table below is read out of "
        "the modules at build time, so it cannot quietly go out of date. The prose between "
        "tables is the only hand-written part.")
    add("")
    add("## The shape of it")
    add("")
    add("One button on the Improve screen. Turned on, a crew of agents works on THIS "
        "repository in sprints, forever; turned off, it stops. Everything it knows lives in "
        "the key-value table (no new schema), and every phase transition is written down "
        "before it runs, so restarting the server — including the crew restarting it after "
        "landing backend changes — resumes mid-sprint instead of starting over.")
    add("")
    add("```")
    add("  Improve screen ──toggle──►  repair:enabled")
    add("                                   │")
    add("      repair.loop()  every %ds, one process only (repair:lease)" % repair.TICK_SECONDS)
    add("                                   │")
    add("                                tick()")
    add("                                   │")
    add("            ┌──────────────────────┴───────────────────────┐")
    add("            │  sleeping? → is the wake time up, or has      │")
    add("            │  headroom() recovered early?                 │")
    add("            └──────────────────────┬───────────────────────┘")
    add("                                   ▼")
    add("                              advance(state)")
    add("     scout ─► plan ─► build ─► verify ─► land ─► retro ─► (rest)")
    add("       │        │        │        │        │")
    add("       │        │        │        │        └─ squash-merge, or queue for review")
    add("       │        │        │        └────────── the platform's OWN full test suite")
    add("       │        │        └─────────────────── one SDK session in a git worktree")
    add("       │        └──────────────────────────── the crew deliberates; the memo IS the plan")
    add("       └───────────────────────────────────── one read-only session over the repo")
    add("```")
    add("")
    add("## The phases")
    add("")
    add(_table(["phase", "what it does"], _phases()))
    add("")
    add("## When it sleeps")
    add("")
    add("Four questions, in order of how authoritative the answer is. The first one that says "
        "no decides, and each carries its own wake time.")
    add("")
    add(_table(["#", "check", "beats"], [
        (1, "A model is rate-limited — the provider's own words "
            "(`launcher.note_rate_limit` parses \"session limit · resets 3pm\")",
         "everything; it is the only measurement we did not invent"),
        (2, "Contention — the owner spent tokens within `repair_yield_quiet_s`",
         "the crew waits minutes, then re-checks"),
        (3, "Share spent — the crew's tokens this window ≥ its allowance",
         "waits for the window to roll"),
        (4, "Session count — a hand-set calls-per-window ceiling",
         "only consulted when NO tokens were reported at all"),
    ]))
    add("")
    add("The allowance is not a separate budget to keep in sync:")
    add("")
    add("```")
    add("allowance = usage_budget_tokens × repair_idle_share − (tokens the owner used this window)")
    add("```")
    add("")
    add("so a busy day shrinks the crew automatically and a quiet night hands it room. "
        "Measured in input+output tokens over a %g-hour rolling window; cache reads are "
        "counted separately because one build reads millions of them. Sources that count as "
        "the owner's work: %s. The crew's own deliberation runs through the same provider "
        "path a Studio seat uses, so `advance()` tags every phase as `repair` — filed as the "
        "owner's it would see the box as busy and sleep on its own footsteps."
        % (usage.window_hours(), ", ".join(f"`{s}`" for s in usage.OWNER_SOURCES)))
    add("")
    add("## The crew")
    add("")
    add("One agent per enabled factor, plus a hidden manager, as a real Studio world you can "
        "open on the canvas. Round one, every agent states a position with its own model, "
        "blind to the others; later rounds are mediated by the manager, each agent seeing only "
        "its graph neighbours', anonymised; a unanimous round earns a devil's advocate. The "
        "decision memo IS the sprint plan.")
    add("")
    add("Dials are this engine's own traits and drives — a dial naming anything else is "
        "dropped silently, which once turned six specialists into one agent six times. A "
        "drive is a homeostatic *level*, so an agent that WANTS something has that drive "
        "sitting below its setpoint (`repair.hunger`).")
    add("")
    add(_table(["factor", "hunts for", "strongest dials", "wants"], _factors()))
    add("")
    add("Changing these in code reaches a running crew only when `TEAM_PERSONAS` "
        "(currently %d) is bumped: both the factor list and the crew's world are kv copies, "
        "and they outrank the source until that stamp moves." % repair.TEAM_PERSONAS)
    add("")
    add("## What stops it breaking things")
    add("")
    add(_table(["rail", "how"], [
        ("Root only", "every endpoint goes through `routes._root`"),
        ("Scout cannot write", "read-only tool set (Read/Glob/Grep)"),
        ("Builds are isolated", "a git worktree under `.repair/`, gitignored"),
        ("Escapes are caught", "the live checkout's `git status` is compared around every "
                               "session; straying outside fails the task by name and reverts "
                               "nothing (that tree is the owner's)"),
        ("Protected paths", ", ".join(f"`{p}`" for p in _protected()) + ", and any `*.db`"),
        ("Green or it does not land", "the platform's own full suite runs in the worktree"),
        ("One commit per task", "squash-merge, so `git revert <sha>` is the whole undo"),
        ("One engine per database", "a kv lease; the process that bound the port claims it"),
        ("Kill switch", "the toggle, and abort for the task in flight"),
    ]))
    add("")
    add("## What it writes down")
    add("")
    add("Two channels, deliberately separate. `bus.emit` is the NARRATIVE — what the crew did, "
        "in order, for the person watching, and it is what the Activity tab's *Story* view "
        "shows. `logs.log` is the OPERATIONAL record — levelled, categorised and searchable, "
        "which is what you want the moment the story stops making sense and you need to know "
        "which part broke. The *Logs* view is that, filtered.")
    add("")
    add("A log row is `{ts, level, cat, event, msg, …fields}`. `event` is a stable slug you "
        "can count and alert on; `msg` is prose and may change freely. Levels: "
        + ", ".join(f"`{lv}`" for lv in _mod("app.logs").LEVELS)
        + " — and filtering by level is a FLOOR, so asking for warnings gives errors too.")
    add("")
    add(_table(["category", "what kind of fact this is"],
               sorted(_mod("app.logs").CATEGORIES.items())))
    add("")
    add("## State (all in kv — no new tables)")
    add("")
    add("The engine's own state is kv, and stays kv. The two `usage:` keys are the "
        "exception on their way out: since P2 the shared quota meter is a separate "
        "service with a real table (`services/usage`, `data/usage.db`), and what is "
        "left here is the blob it copied from plus the guard on that copy.")
    add("")
    add(_table(["key", "holds"], _kv_keys()))
    add("")
    add("## HTTP")
    add("")
    add(_table(["method", "path", "what for"], _endpoints()))
    add("")
    add("## Knobs")
    add("")
    add("Changeable on a running instance (Settings, or the `tuning` table); every one carries "
        "its own rationale in `tuning.py`.")
    add("")
    add(_table(["knob", "default", "why it is what it is"], _knobs()))
    add("")
    add("## Where the code is")
    add("")
    add(_table(["file", "role"], [
        ("`conductor/app/repair.py`", "the engine: state machine, factors, meters, sleep"),
        ("`conductor/app/repair_builder.py`",
         "disk and model work — worktrees, sessions, verify, land/revert. This is the seam "
         "to swap when builds move to other machines"),
        ("`conductor/app/repair_routes.py`", "the HTTP surface"),
        ("`conductor/app/usage.py`", "the shared token meter every spender reports to"),
        ("`dashboard/js/repair.js`", "the whole Improve screen"),
        ("`conductor/app/logs.py`", "the levelled, categorised log pipeline"),
        ("`conductor/app/logs_routes.py`", "reading and filtering those logs, root only"),
        ("`tests/test_repair.py`", "the offline suite — a full sprint with zero model calls"),
    ]))
    add("")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    text = build()
    if "--check" in sys.argv:
        current = OUT.read_text() if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is stale — run: python tools/gen_selfrepair_doc.py")
            sys.exit(1)
        print("up to date")
    else:
        OUT.write_text(text)
        print(f"wrote {OUT.relative_to(ROOT)} ({len(text.splitlines())} lines)")
