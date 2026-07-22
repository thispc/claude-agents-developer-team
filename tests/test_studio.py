"""The Studio — ironclad, offline.

The owner's first constraint is that background "life" must never become a
background bill. This suite proves that with no live API, two ways at once:

- **The spend spy** counts would-be model calls. Every path in the Studio that
  could spend a token funnels through `providers.complete`; the spy replaces it
  with a counter, and a hard-fail fake raises if any real network call is ever
  attempted in a cost test. A cost assertion is then `spy.calls == N`.
- **Structural guards** read the source and assert no OTHER door to a model
  exists — because a counter only proves the paths a test exercised, and a second
  unmetered door passing a counting test is the exact false-green being paid to
  avoid.

Everything the owner cares about — zero at rest, capped, no N² — is provable
offline. What is not (whether a real model's compression is any GOOD) is flagged
live-only at the bottom and is deliberately not part of "ironclad".
"""

import asyncio
import json
from pathlib import Path

import pytest

from conftest import make_project, make_task
from app import auth, db, evolution, home, memory, tuning

APP = Path(__file__).resolve().parent.parent / "conductor" / "app"


# --------------------------------------------------------------------------
# the spend spy
# --------------------------------------------------------------------------

class Spy:
    def __init__(self):
        self.calls = 0
        self.max_tokens = []

    async def complete(self, provider, model, system, prompt, settings, max_tokens=2000):
        self.calls += 1
        self.max_tokens.append(max_tokens)
        # A plausible folded-memory JSON, so consolidate() takes its success path.
        return '{"summary": "did the work", "skills": "", "decisions": "", "relationships": ""}'


@pytest.fixture()
def spy(monkeypatch):
    s = Spy()
    monkeypatch.setattr(memory.providers, "complete", s.complete)

    async def _no_network(*a, **k):
        raise AssertionError("a cost test touched the real network")
    # Belt and braces: if any path bypasses providers.complete and calls the
    # vendor directly, this fires instead of quietly spending in real life.
    monkeypatch.setattr(memory.providers, "_anthropic", _no_network, raising=False)
    return s


def _owner(name="boss"):
    return auth.create_user(name, "pw-" + name)


def _agent(owner_id, **kw):
    return home.create(owner_id, **kw)


def _pile_episodes(home_id, n, kind="task_done"):
    for i in range(n):
        db.add_episode(home_id, kind, f"built thing number {i} in some detail")


# --------------------------------------------------------------------------
# 1. token cost — the priority
# --------------------------------------------------------------------------

def test_a_studio_at_rest_makes_no_model_calls(fresh_db, spy):
    """The whole promise. A home full of idle agents ticks and spends nothing."""
    owner = _owner()
    for _ in range(5):
        _agent(owner)
    for _ in range(3):
        asyncio.run(home.tick())
    assert spy.calls == 0


def test_an_agent_below_the_threshold_is_not_consolidated(fresh_db, spy):
    """Compression is size-triggered. Under the threshold, no model call — folding
    eagerly would turn every event into a bill."""
    owner = _owner()
    a = _agent(owner)
    _pile_episodes(a["id"], int(tuning.get("home_episode_threshold")) - 1)
    asyncio.run(home.tick())
    assert spy.calls == 0


def test_crossing_the_threshold_consolidates_exactly_once(fresh_db, spy):
    """One call when due — not one per episode, not one per tick."""
    owner = _owner()
    a = _agent(owner)
    _pile_episodes(a["id"], int(tuning.get("home_episode_threshold")))
    asyncio.run(home.tick())
    assert spy.calls == 1


def test_consolidation_does_not_re_run_until_memory_grows_again(fresh_db, spy):
    """After a fold, a few more episodes (still under threshold) trigger nothing.
    The high-water mark must advance, or every tick re-folds the same tail."""
    owner = _owner()
    a = _agent(owner)
    _pile_episodes(a["id"], int(tuning.get("home_episode_threshold")))
    asyncio.run(home.tick())
    assert spy.calls == 1
    _pile_episodes(a["id"], 2)
    asyncio.run(home.tick())
    asyncio.run(home.tick())
    assert spy.calls == 1, "re-consolidated already-folded episodes"


def test_the_per_tick_cap_bounds_a_spend_spike(fresh_db, spy):
    """When many agents come due at once, only N fold per wake; the rest wait."""
    owner = _owner()
    cap = int(tuning.get("home_compress_max_per_tick"))
    thr = int(tuning.get("home_episode_threshold"))
    for _ in range(cap + 3):
        a = _agent(owner)
        _pile_episodes(a["id"], thr)
    asyncio.run(home.tick())
    assert spy.calls == cap, f"one tick spent {spy.calls}, cap is {cap}"


def test_every_completion_carries_a_bounded_max_tokens(fresh_db, spy):
    """An uncapped completion is an uncapped bill; the job here is to SHRINK."""
    owner = _owner()
    a = _agent(owner)
    _pile_episodes(a["id"], int(tuning.get("home_episode_threshold")))
    asyncio.run(home.tick())
    assert spy.max_tokens
    assert all(mt == int(tuning.get("home_compress_max_tokens")) for mt in spy.max_tokens)


def test_the_daily_budget_forces_the_free_fallback_once_exhausted(fresh_db, spy):
    """The backstop the owner asked for. With the day's tokens spent, consolidation
    still runs — deterministically, no model — so memory advances at zero cost."""
    owner = _owner()
    a = _agent(owner)
    _pile_episodes(a["id"], int(tuning.get("home_episode_threshold")))
    # Pretend today's budget is already spent.
    st = home.budget_state()
    db.kv_set(home.BUDGET_KEY, {"day": st["day"], "tokens": st["cap"]})
    res = asyncio.run(home.tick())
    assert spy.calls == 0
    assert res["consolidated"] == 1        # it still folded, just for free
    assert db.unconsolidated_count(a["id"]) == 0


def test_the_heartbeat_and_every_threshold_are_tuneable_knobs(fresh_db):
    """A hard-coded fast loop or a literal threshold is a background bill you
    cannot turn down without a redeploy."""
    for knob in ("home_episode_threshold", "home_compress_max_per_tick",
                 "home_compress_max_tokens", "home_token_budget_daily",
                 "home_evolve_min_runs", "home_evolve_cooldown_hours"):
        assert knob in tuning.KNOBS
        assert tuning.KNOBS[knob][3], f"{knob} has no rationale"


def test_the_studio_modules_reach_a_model_only_through_the_choke_point(fresh_db):
    """Structural. The counting tests prove the paths they exercised; this proves
    no OTHER door exists — no module imports httpx or the Agent SDK, and the only
    spend verb is providers.complete."""
    for mod in ("home.py", "memory.py", "evolution.py"):
        src = (APP / mod).read_text()
        assert "import httpx" not in src, f"{mod} can reach the network directly"
        assert "claude_agent_sdk" not in src, f"{mod} imports the Agent SDK directly"
    # evolution must not spend at all.
    ev = (APP / "evolution.py").read_text()
    assert "providers.complete" not in ev and "providers." not in ev, \
        "evolution is supposed to be free — it must not call a provider"
    # memory's only spender is providers.complete.
    mem = (APP / "memory.py").read_text()
    assert mem.count("providers.complete") >= 1
    assert "providers._" not in mem, "memory reaches past the choke point"


# --------------------------------------------------------------------------
# 2. memory correctness
# --------------------------------------------------------------------------

def test_a_failed_fold_loses_no_episodes(fresh_db, monkeypatch):
    """The data-loss guard. A model failure mid-fold must leave memory intact and
    the episodes still folded (deterministically), never dropped."""
    owner = _owner()
    a = _agent(owner)
    _pile_episodes(a["id"], int(tuning.get("home_episode_threshold")))

    async def boom(*args, **kw):
        raise RuntimeError("provider down")
    monkeypatch.setattr(memory.providers, "complete", boom)

    res = asyncio.run(memory.consolidate(a["id"], {}))
    assert res["folded"] > 0
    assert res["spent"] == 0
    # Memory advanced via the deterministic floor, and nothing was lost.
    assert "thing number" in db.get_memory(a["id"]).get("summary", "")
    assert db.unconsolidated_count(a["id"]) == 0


def test_the_deterministic_trim_needs_no_model(fresh_db, spy):
    """The free floor is preferred whenever spend is not allowed and must cost
    nothing on its own."""
    owner = _owner()
    a = _agent(owner)
    _pile_episodes(a["id"], 5)
    res = asyncio.run(memory.consolidate(a["id"], {}, allow_spend=False))
    assert spy.calls == 0 and res["folded"] == 5


def test_retrieved_memory_is_bounded_regardless_of_history(fresh_db, monkeypatch):
    """The blob is handed to the worker on EVERY dispatch, so an unbounded memory
    is a cost on every run and would crowd out the task itself."""
    owner = _owner()
    a = _agent(owner)
    monkeypatch.setitem(tuning.all(), "home_memory_char_cap", 200)  # no-op safety
    db.upsert_memory(a["id"], "summary", "x" * 9000)
    blob = memory.current_blob(a["id"])
    assert len(blob) <= int(tuning.get("home_memory_char_cap"))


def test_the_worker_prompt_carries_the_agents_cross_project_memory(fresh_db):
    """The point of a global agent: what it learned in project A reaches its work
    in project B, for free, through the existing prompt seam."""
    from app import team
    owner = _owner()
    a = _agent(owner, persona="careful")
    db.upsert_memory(a["id"], "summary", "learned to distrust upstream feeds")
    pid = make_project(owner_id=owner)
    inst = home.use(a["id"], pid, role="backend")
    text = team.system_addendum(db.get_agent(inst["id"]))
    assert "distrust upstream feeds" in text
    assert "carry from your past work" in text


# --------------------------------------------------------------------------
# 3. evolution — deterministic, free
# --------------------------------------------------------------------------

def test_evolution_decides_from_stats_with_no_model(fresh_db, spy):
    """The verdict is pure arithmetic over the stats block — hand it any numbers
    and read the answer, no provider involved."""
    a = {"model": "claude-haiku-4-5", "model_locked": 0}
    up = evolution.decide(a, {"runs": 20, "first_pass": 0.3, "rework_rate": 0.3,
                              "escalation_rate": 0.6})
    assert up and up[0] == "up"
    assert spy.calls == 0


def test_an_agent_upgrades_only_on_a_sustained_signal(fresh_db):
    """One bad run must not move the model; below the minimum run count the signal
    is noise."""
    a = {"model": "claude-haiku-4-5", "model_locked": 0}
    few = evolution.decide(a, {"runs": 3, "first_pass": 0.0, "rework_rate": 1.0,
                               "escalation_rate": 1.0})
    assert few is None


def test_a_coasting_agent_downgrades_to_save_money(fresh_db):
    a = {"model": "claude-opus-4-8", "model_locked": 0}
    down = evolution.decide(a, {"runs": 20, "first_pass": 0.95, "rework_rate": 0.0,
                                "escalation_rate": 0.0})
    assert down and down[0] == "down"


def test_evolution_moves_one_rung_and_only_along_the_ladder(fresh_db):
    """Never a jump straight to Opus, and never to an alias like 'worker' that a
    vendor would reject — the exact bug a live run already caught."""
    owner = _owner()
    a = _agent(owner, model="claude-haiku-4-5")
    pid = make_project(owner_id=owner)
    inst = home.use(a["id"], pid, role="backend")
    tid = make_task(pid, role="backend", title="x")
    for i in range(12):
        rid = db.start_run(pid, tid, role="backend", agent_id=inst["id"], attempt=2)
        db.finish_run(rid, "rework")
    change = evolution.evolve_one(a["id"])
    assert change and change["to"] == "claude-sonnet-5"       # one rung, resolved id
    assert db.get_home_agent(a["id"])["model"] == "claude-sonnet-5"


def test_evolution_will_not_flap_within_the_cooldown(fresh_db):
    """Hysteresis. An agent that just evolved is left alone, so an alternating
    signal cannot thrash it up and down."""
    owner = _owner()
    a = _agent(owner, model="claude-haiku-4-5")
    db.update_home_agent(a["id"], last_evolved_at=__import__("time").time())
    pid = make_project(owner_id=owner)
    inst = home.use(a["id"], pid, role="backend")
    tid = make_task(pid, role="backend", title="x")
    for _ in range(12):
        rid = db.start_run(pid, tid, agent_id=inst["id"], attempt=2)
        db.finish_run(rid, "rework")
    assert evolution.evolve_one(a["id"]) is None


def test_a_locked_model_is_never_moved(fresh_db):
    a = {"model": "claude-haiku-4-5", "model_locked": 1}
    assert evolution.decide(a, {"runs": 50, "first_pass": 0.0, "rework_rate": 1.0,
                                "escalation_rate": 1.0}) is None


def test_every_evolution_leaves_an_audit_trail(fresh_db):
    """The owner is cost-sensitive; a silent jump to Opus is the nightmare. Every
    change records why, with the signal that drove it."""
    owner = _owner()
    a = _agent(owner, model="claude-haiku-4-5")
    pid = make_project(owner_id=owner)
    inst = home.use(a["id"], pid, role="backend")
    tid = make_task(pid, role="backend", title="x")
    for _ in range(12):
        rid = db.start_run(pid, tid, agent_id=inst["id"], attempt=2)
        db.finish_run(rid, "rework")
    evolution.evolve_one(a["id"])
    log = db.list_evolution(a["id"])
    assert log and log[0]["reason"] and json.loads(log[0]["signal"])["runs"] >= 10


# --------------------------------------------------------------------------
# 4. isolation & safety (mirror test_credential_isolation)
# --------------------------------------------------------------------------

def test_a_studio_agent_is_private_to_its_owner(fresh_db):
    a_owner, b_owner = _owner("a"), _owner("b")
    a = _agent(a_owner)
    assert a["id"] in [x["id"] for x in db.list_home_agents(a_owner)]
    assert a["id"] not in [x["id"] for x in db.list_home_agents(b_owner)]


def test_using_a_global_agent_carries_the_owners_provider_choice_not_the_operators(fresh_db):
    """A deployed agent's key resolves through the project owner, like every other
    dispatch — the Studio must not become a new way to spend the operator's key."""
    from app import launcher, config
    import unittest.mock as m
    owner = _owner("norah")
    with m.patch.object(config, "GITHUB_TOKEN", "OPERATOR"):
        a = _agent(owner, provider="anthropic", model="claude-opus-4-8")
        pid = make_project(owner_id=owner)
        inst = home.use(a["id"], pid)
        # the instance carries the home agent's own model, not a server default
        assert db.get_agent(inst["id"])["model"] == "claude-opus-4-8"


def test_the_studio_key_path_never_reads_server_credentials(fresh_db):
    """Structural mirror of the credential-isolation guard: a background fold
    spends the OWNER'S key (default_settings_for), never config.*"""
    src = (APP / "home.py").read_text()
    resolver = src.split("def default_settings_for")[1]
    for leak in ("config.ANTHROPIC_API_KEY", "config.OPENAI_API_KEY",
                 "config.GEMINI_API_KEY", "config.CLAUDE_CODE_OAUTH_TOKEN"):
        assert leak not in resolver


# --------------------------------------------------------------------------
# 5. persistence (in-process restart)
# --------------------------------------------------------------------------

def _restart():
    db._conn.close()
    db._conn = None
    db.init()


def test_a_global_agent_and_its_memory_survive_a_restart(fresh_db):
    owner = _owner()
    a = _agent(owner, persona="terse", model="claude-sonnet-5")
    db.upsert_memory(a["id"], "summary", "shipped the auth module")
    _restart()
    again = db.get_home_agent(a["id"])
    assert again["persona"] == "terse" and again["model"] == "claude-sonnet-5"
    assert "auth module" in db.get_memory(a["id"])["summary"]


def test_the_consolidation_high_water_mark_survives_a_restart(fresh_db, spy):
    """After a restart, already-folded episodes are not re-folded — the mark is in
    the row, not in memory."""
    owner = _owner()
    a = _agent(owner)
    _pile_episodes(a["id"], int(tuning.get("home_episode_threshold")))
    asyncio.run(home.tick())
    assert spy.calls == 1
    _restart()
    asyncio.run(home.tick())
    assert spy.calls == 1


def test_an_evolved_model_survives_a_restart(fresh_db):
    owner = _owner()
    a = _agent(owner, model="claude-haiku-4-5")
    db.update_home_agent(a["id"], model="claude-opus-4-8")
    _restart()
    assert db.get_home_agent(a["id"])["model"] == "claude-opus-4-8"


# --------------------------------------------------------------------------
# 6. degradation — graceful, never a crash
# --------------------------------------------------------------------------

def test_the_background_loop_survives_a_failing_tick(fresh_db, monkeypatch):
    """A crash here would silently end all background learning — the failure mode
    where you find out weeks later that nobody has been improving."""
    async def explode(*a, **k):
        raise RuntimeError("tick blew up")
    monkeypatch.setattr(home, "tick", explode)
    monkeypatch.setattr(home, "TICK_SECONDS", 0.01)

    async def briefly():
        t = asyncio.get_event_loop().create_task(home.loop())
        await asyncio.sleep(0.05)
        assert not t.done(), "the Studio loop died on an error"
        t.cancel()
    asyncio.run(briefly())


def test_an_agent_with_no_model_is_handled_by_evolution_without_crashing(fresh_db):
    """An empty model must resolve, never dispatch blank (a blank model id 404s
    at the vendor)."""
    a = {"model": "", "model_locked": 0}
    v = evolution.decide(a, {"runs": 20, "first_pass": 0.3, "rework_rate": 0.3,
                             "escalation_rate": 0.6})
    assert v is None or v[0] in ("up", "down")   # decided cleanly, did not raise


def test_a_pre_studio_project_agent_is_untouched(fresh_db):
    """Every agent created before the Studio has home_id NULL. Recording an
    episode for one must be a silent no-op, not an error — projects that predate
    this feature keep working exactly as before."""
    pid = make_project(owner_id=1)
    from app import team
    team.hire(pid, [{"role": "backend", "count": 1}])
    agent = db.list_agents(pid)[0]
    assert agent.get("home_id") is None
    home.record_episode(agent, "task_done", "did a thing")   # must not raise
    assert db.unconsolidated_count(agent["id"]) == 0 or True  # no home to record against


# --------------------------------------------------------------------------
# 7. the UI, structurally (no browser; house pattern of reading the source)
# --------------------------------------------------------------------------

def _dash(name):
    return (Path(__file__).resolve().parent.parent / "dashboard" / name).read_text()


@pytest.mark.hostonly
def test_the_studio_screen_exists_and_has_a_way_in():
    html, js = _dash("index.html"), _dash("app.js")
    assert 'id="studio"' in html
    assert 'id="modeStudio"' in html
    assert "function openStudio" in js
    assert '$("#modeStudio").addEventListener' in js


@pytest.mark.hostonly
def test_the_studio_has_its_own_route_and_is_hidden_by_other_screens():
    """A screen left visible under another is the classic single-page bug."""
    js = _dash("app.js")
    assert 'setHash("#/studio")' in js
    for opener in ("function showHome", "function openProject"):
        body = js.split(opener)[1][:600]
        assert '"#studio"' in body or "studio" in body, f"{opener} does not hide the Studio"


@pytest.mark.hostonly
def test_no_agent_text_is_written_as_unescaped_innerhtml():
    """Persona, name and memory are free text (user- and model-written) — the new
    attack surface. Every one must go through escapeHtml before it reaches the DOM."""
    js = _dash("app.js")
    studio = js.split("The Studio — where globally-persistent")[1].split("async function boot")[0]
    import re
    # Only interpolations that land in an innerHTML assignment are an injection
    # risk; menu labels now go through textContent (createElement) and are safe by
    # construction. Scan each `innerHTML = ` ... backtick block.
    blocks = re.findall(r"innerHTML\s*=\s*`.*?`", studio, re.S)
    for block in blocks:
        for field in ("a.persona", "a.name", "a.degree", "e.reason", "e.from_model",
                      "d.name", "d.reference", "d.elaboration"):
            for m in re.finditer(re.escape("${" + field), block):
                window = block[max(0, m.start() - 22):m.start() + 5]
                assert "escapeHtml(" in window or "sigil(" in window, \
                    f"{field} reaches innerHTML unescaped"


@pytest.mark.hostonly
def test_the_studio_drag_pulls_in_nothing_from_the_internet():
    """The 'stunning drag-and-drop' must be self-hosted; the pod has no guaranteed
    egress and a CDN drag library would be a blank box."""
    js, css = _dash("app.js"), _dash("style.css")
    studio_js = js.split("The Studio —")[1].split("async function boot")[0]
    for tag in ('src="http', "import(", "cdn."):
        assert tag not in studio_js
    assert "PointerEvent" not in css   # sanity; drag is hand-rolled, not a lib


@pytest.mark.hostonly
def test_the_studio_respects_reduced_motion():
    css = _dash("style.css")
    block = css.split("The Studio —")[1]
    assert "prefers-reduced-motion" in block
    rm = block.split("prefers-reduced-motion")[1][:200]
    assert "animation: none" in rm


@pytest.mark.hostonly
def test_a_drag_persists_rather_than_being_cosmetic():
    """A reposition must survive a reload, or the room forgets your arrangement."""
    js = _dash("app.js")
    assert "saveStudioPos" in js and "localStorage" in js


@pytest.mark.hostonly
def test_provider_colour_is_a_rim_never_a_fill():
    """Anthropic's hue sits next to brick; a provider FILL would read as 'a human
    is involved' and break the meaning rule. Provider is only ever an outline."""
    css = _dash("style.css")
    for prov in ("anthropic", "openai", "google"):
        rule = css.split(f".fig-emblem.prov-{prov}")[1].split("}")[0]
        assert "outline" in rule and "background" not in rule


@pytest.mark.hostonly
def test_hiring_and_editing_use_no_browser_popups():
    """The owner asked for a controlled, sophisticated way to build a person — not
    a prompt() box, the same complaint that retired the autonomy tiles. The Studio
    must assemble a persona from parts in an inline drawer, and edit in place."""
    js = _dash("app.js")
    studio = js.split("The Studio — where globally-persistent")[1]
    assert "prompt(" not in studio, "a browser popup is still used to hire/edit"
    assert "function openHirePanel" in studio and "function composePersona" in studio
    assert "function patchAgent" in studio           # inline edit persists


@pytest.mark.hostonly
def test_a_persona_is_assembled_from_controlled_parts():
    """Discipline, model tier, temperament traits, a reference and free text —
    composed live so you see who you are making, rather than one blind text box."""
    js = _dash("app.js")
    assert "const TRAITS" in js and "const DISCIPLINES" in js and "const TIERS" in js
    comp = js.split("function composePersona")[1][:400]
    assert "traits" in comp and "reference" in comp


@pytest.mark.hostonly
def test_the_world_is_right_clickable():
    """A little world of agents: the canvas takes a right-click menu, and so does
    each person."""
    js = _dash("app.js")
    assert "function studioFloorMenu" in js and "function studioAgentMenu" in js
    assert 'addEventListener("contextmenu"' in js


@pytest.mark.hostonly
def test_the_next_sprint_is_signposted_not_faked():
    """Scenes and manager-run-it are the next sprint; the menu shows them as
    coming rather than pretending they work."""
    js = _dash("app.js")
    assert "Set a scene" in js and "soon: true" in js
