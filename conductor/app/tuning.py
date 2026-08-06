"""The orchestration knobs, in one place and changeable on a running instance.

Every value here used to be a literal somewhere in `launcher`, `manager`,
`scheduler` or `blockers`. That was fine while the algorithm was fixed and wrong
the moment it wasn't: deciding whether to escalate after one failed attempt or
two is exactly the kind of question you answer by trying both and looking at the
numbers, and you cannot try both if changing one means a rebuild, a push, and a
rollout of the thing you are experimenting on.

Resolution order, cheapest to override last:

    DEFAULTS  <  environment variable  <  the `tuning` table

The environment layer keeps existing deployments behaving exactly as they did
(the env names are the ones already in use). The table layer is what a human or
the platform itself edits at runtime, and it is the only one that survives a
config change without a redeploy.

Every knob carries a `why` — not documentation for its own sake, but because a
number with no rationale gets tuned by vibes, and six months later nobody can say
whether 2 was measured or guessed.
"""

from typing import Any

from . import config, db

# name -> (default, kind, env var, why it is set where it is)
KNOBS: dict[str, tuple[Any, type, str, str]] = {
    # --- when to spend a bigger model ---
    "escalate_after_attempts": (
        2, int, "ESCALATE_AFTER_ATTEMPTS",
        "Failed attempts on one task before moving up the model ladder. Lower "
        "burns money on work a cheap model was never going to finish; higher "
        "burns wall-clock re-running something that keeps failing the same way."),
    "escalate_on_rate_limit_after": (
        1, int, "ESCALATE_ON_RATE_LIMIT_AFTER",
        "A rate limit is not a quality failure, so it escalates sooner — the "
        "point is to reach a model that will answer, not a better one."),
    "max_attempts": (
        3, int, "TASK_MAX_ATTEMPTS",
        "Attempts before the manager is told a task is stuck. Repeated identical "
        "failure is a planning problem, and more attempts do not fix planning."),

    # --- contests ---
    "contest_max_width": (
        3, int, "CONTEST_MAX_WIDTH",
        "Ceiling on rival attempts at one task. Width buys diversity, and the "
        "evidence is that diversity is worth far less than good aggregation — so "
        "this stays small and the effort goes into judging."),
    "contest_min_deliverables": (
        2, int, "CONTEST_MIN_DELIVERABLES",
        "Rivals that must have actually produced something before a contest is "
        "judged rather than salvaged. Selecting from a pool of one is not selection."),

    # --- review ---
    "review_panel_size": (
        1, int, "REVIEW_PANEL_SIZE",
        "How many teammates weigh in on finished work besides the manager. The "
        "aggregation step is where multi-agent review earns its cost; 1 means "
        "the manager still decides but hears one informed second opinion."),
    "review_requires_evidence": (
        True, bool, "REVIEW_REQUIRES_EVIDENCE",
        "Refuse to accept work whose tests were never run. An imperfect verifier "
        "caps achievable accuracy no matter how much inference you buy, so the "
        "verifier is the thing worth protecting."),

    # --- pacing ---
    "max_concurrent_workers": (
        config.MAX_CONCURRENT_WORKERS, int, "MAX_CONCURRENT_WORKERS",
        "Agents in flight per project. Bounded by API rate limits far more often "
        "than by anything on this machine."),
    "stuck_seconds": (
        1800, int, "WORKER_STUCK_SECONDS",
        "Silence from a running agent before it is presumed dead. Long, because a "
        "real build legitimately goes quiet for a while."),
    "slow_seconds": (
        900, int, "SLOW_SECONDS",
        "When a task starts being called slow in the blockers panel. Advisory only."),

    # --- looking after itself ---
    "upkeep_enabled": (
        True, bool, "UPKEEP_ENABLED",
        "Run the daily self-check. Scanning and ranking cost nothing — they only "
        "read what was already recorded — so this is on by default. What the check "
        "is then allowed to DO is governed separately below."),
    "upkeep_interval_hours": (
        24, int, "UPKEEP_INTERVAL_HOURS",
        "Hours between self-checks. Daily is the useful cadence: often enough that "
        "a fault is not a week old when you hear about it, rare enough that the "
        "report is worth reading."),
    "upkeep_files_tickets": (
        True, bool, "UPKEEP_FILES_TICKETS",
        "Let the check file the worst finding as a ticket. Filing is cheap and "
        "reversible; it tells a human what is wrong without deciding anything."),
    "repair_score_floor": (
        400.0, float, "REPAIR_SCORE_FLOOR",
        "How bad a finding must score before it is worth anyone's attention. A "
        "knob rather than a constant because the right value depends on how noisy "
        "a given deployment is, which nobody can know in advance. Roughly: one "
        "critical fault seen a few times, or a warning seen very many."),

    # --- the boss's involvement ---
    "interview_questions": (
        3, int, "INTERVIEW_QUESTIONS",
        "Questions the manager may ask about the brief before it plans. Zero "
        "switches the interview off entirely. Kept small because each one is an "
        "interruption, and three good questions asked together beat six asked one "
        "at a time."),
    "interview_wait_seconds": (
        600, int, "INTERVIEW_WAIT_SECONDS",
        "How long it waits for those answers before planning anyway. Unlike the "
        "sprint check-in this DOES block, because the plan's shape depends on the "
        "answers and a late answer either invalidates the plan or gets ignored. "
        "Bounded so an unattended run still starts; autonomous projects skip the "
        "wait entirely."),
    "sprint_checkin_seconds": (
        0, int, "SPRINT_CHECKIN_SECONDS",
        "How long a sprint boundary waits for the boss before planning the next "
        "cycle. Zero by default: the question is still posted and the answer is "
        "still read when it arrives, but an unattended overnight run must not stall "
        "an hour per sprint waiting for someone who is asleep. Raise it when you "
        "intend to be present."),

    # --- the Studio: globally-persistent agents ---
    #
    # Every threshold here is a knob and not a literal for one reason above all: the
    # owner's first constraint is that background "life" must not become a background
    # bill, and the way you keep that promise on a running instance — without a
    # rebuild — is to make every door to a token spend adjustable and self-documented.
    "home_life_enabled": (
        True, bool, "HOME_LIFE_ENABLED",
        "Run the free background tick that decides when — rarely — an agent should "
        "spend. The tick itself reads rows and calls no model, so this is on by "
        "default; what it is ALLOWED to spend on is capped separately below."),
    "home_episode_threshold": (
        12, int, "HOME_EPISODE_THRESHOLD",
        "Unconsolidated episodes an agent accumulates before its memory is folded "
        "down. Size, never time: an idle agent accumulates nothing and is never due, "
        "which is what makes memory cost proportional to work rather than to the "
        "clock. Lower means fresher memory and more frequent cheap calls; higher "
        "means cheaper and coarser."),
    "home_memory_char_cap": (
        4000, int, "HOME_MEMORY_CHAR_CAP",
        "Hard ceiling on an agent's long-term memory. This is what the worker is "
        "handed every dispatch, so it is a token cost on EVERY run — a memory that "
        "grows without bound would crowd out the task it is meant to inform."),
    "home_compress_model": (
        "claude-haiku-4-5", str, "HOME_COMPRESS_MODEL",
        "Which model folds memory. The cheap tier, always: summarising short gists "
        "into a shorter blob is exactly the work a small model does well, and this "
        "is the only recurring background spend the Studio has."),
    "home_compress_max_tokens": (
        512, int, "HOME_COMPRESS_MAX_TOKENS",
        "Output ceiling on the one consolidation call. An uncapped completion is an "
        "uncapped bill, and the job here is to SHRINK, so a small budget is correct."),
    "home_compress_max_per_tick": (
        3, int, "HOME_COMPRESS_MAX_PER_TICK",
        "Agents consolidated per wake. Bounds the spend spike when many come due at "
        "once — the surplus simply waits for the next tick rather than billing all "
        "at once."),
    "home_compress_cooldown_minutes": (
        60, int, "HOME_COMPRESS_COOLDOWN_MINUTES",
        "Minimum gap between one agent's consolidations, so a burst of work cannot "
        "trigger back-to-back model calls on the same agent."),
    "home_evolve_enabled": (
        True, bool, "HOME_EVOLVE_ENABLED",
        "Let an agent move up or down the model ladder from its recorded runs. The "
        "decision is pure arithmetic over rows already written — zero tokens — so it "
        "is on by default, like the free daily self-check."),
    "home_evolve_min_runs": (
        10, int, "HOME_EVOLVE_MIN_RUNS",
        "Recent terminal runs required before evolution will move a model. Below "
        "this the signal is noise, and a model changed on three data points is a "
        "coin flip dressed as a decision."),
    "home_evolve_cooldown_hours": (
        24, int, "HOME_EVOLVE_COOLDOWN_HOURS",
        "Minimum dwell between an agent's model changes. The hysteresis that stops "
        "an agent flapping up and down on an alternating signal, which would thrash "
        "spend and make its run history incomparable."),
    "home_standup_enabled": (
        False, bool, "HOME_STANDUP_ENABLED",
        "Let agents exchange what they know in ONE aggregated moderator call over "
        "their existing memory — never N² conversations. Off by default because, "
        "unlike the rest of the Studio, it bills."),
    "home_token_budget_daily": (
        50000, int, "HOME_TOKEN_BUDGET_DAILY",
        "Hard ceiling on tokens all background Studio activity may spend in a day. "
        "The backstop the owner asked for: when it is reached, consolidation and "
        "standup fall back to their deterministic form and the free parts carry on, "
        "so background life has a fixed, low, visible daily cap — and that cap is 0 "
        "when nothing is happening."),

    # --- scenes: a setting where artifacts (code) shape what agents do ---
    #
    # A scene extends the Studio's first rule without exception: at rest it costs
    # zero, the machinery (dealer, effects, turn order) is deterministic code, and
    # only an agent's utterance bills. Every door to a spend is a bounded, adjustable
    # knob so a match cannot become a runaway bill on a running instance.
    "scene_enabled": (
        True, bool, "SCENE_ENABLED",
        "Allow scenes at all. Loading, seating, dealing and advancing a turn order "
        "are free code, so the surface is on by default; what may SPEND is capped "
        "below and by each scene's own token budget."),
    "scene_token_budget_default": (
        20000, int, "SCENE_TOKEN_BUDGET_DEFAULT",
        "The token ceiling a new scene carries. When it is reached the match pauses "
        "rather than running up a bill — the same visible backstop the daily Studio "
        "budget is. Sized for a hand of poker: a brief and an action per player, "
        "each bounded, with headroom."),
    "scene_utterance_max_tokens": (
        200, int, "SCENE_UTTERANCE_MAX_TOKENS",
        "Output ceiling on one agent's turn or reply. A scene utterance is a choice "
        "and a line of character, never an essay — a small cap keeps the per-turn "
        "cost fixed and known, which is what makes a whole match O(turns) in tokens."),
    "scene_brief_max_tokens": (
        160, int, "SCENE_BRIEF_MAX_TOKENS",
        "Output ceiling on one manager briefing as it walks to a player. Smaller "
        "than a turn: a briefing is a nudge, and there is exactly one per player, so "
        "the manager's whole walk is O(players) bounded calls."),
    "scene_default_model": (
        "claude-haiku-4-5", str, "SCENE_DEFAULT_MODEL",
        "Model a seated agent uses when it carries none of its own. The cheap tier: "
        "a character deciding fold-or-call and saying one line is exactly the light "
        "work a small model does well, and a scene should never be the expensive "
        "thing in the room."),

    # --- work sharing ---
    "reuse_verification": (
        True, bool, "REUSE_VERIFICATION",
        "Let a task inherit a teammate's already-run verification of the same "
        "commit instead of re-running it. Re-running a green suite costs a full "
        "agent dispatch and tells you what you already knew."),
    "rebalance_idle": (
        True, bool, "REBALANCE_IDLE",
        "Offer blocked-but-idle teammates work outside their role rather than "
        "leaving them parked while one critical path runs alone."),

    # --- self-repair v2: the IT crew (repair.py) ---
    "repair_tasks_per_sprint": (
        2, int, "REPAIR_TASKS_PER_SPRINT",
        "Improvements the crew may build per sprint. More parallelises badly "
        "against the same session caps and multiplies full-suite verifies; one "
        "makes sprints feel stalled."),
    "repair_plan_rounds": (
        2, int, "REPAIR_PLAN_ROUNDS",
        "Deliberation rounds the crew spends picking the sprint's tasks — the "
        "debate literature says consensus solidifies by round 2-3; the thread "
        "protocol's max_rounds caps it regardless."),
    "repair_supervised": (
        False, bool, "REPAIR_SUPERVISED",
        "True parks every green change in a review queue for a human click; "
        "False lands it the moment the full suite is green. The button's promise "
        "is autonomy, so the default is to land."),
    "repair_auto_restart": (
        True, bool, "REPAIR_AUTO_RESTART",
        "Restart the conductor between sprints when landed changes touched "
        "backend code, so improvements take effect immediately instead of "
        "waiting for someone to remember."),
    "repair_session_cap": (
        14, int, "REPAIR_SESSION_CAP",
        "MODEL CALLS the crew may make per 5-hour rolling window — every call, not just the "
        "heavy scout/build sessions: planning and extraction spend real tokens too, and a "
        "meter that reads 5/6 while four other calls went out is not an honest limit. Mirrors "
        "Claude's subscription session ladder so self-repair never starves the owner's own "
        "interactive use. Raised from 6 when the meter started counting everything."),
    "repair_backlog_size": (
        6, int, "REPAIR_BACKLOG_SIZE",
        "Improvements one scout+deliberation plans ahead. The backlog is what makes the loop "
        "affordable: planning costs ~4 calls, so amortising it over 6 builds instead of 1 is "
        "several times the improvements per unit of quota."),
    "repair_backlog_max_age_h": (
        12, int, "REPAIR_BACKLOG_MAX_AGE_H",
        "How long a planned backlog stays trustworthy. The repo moves under it — every landing "
        "is a change the plan did not know about — so it is re-scouted after this."),
    "repair_weekly_cap": (
        140, int, "REPAIR_WEEKLY_CAP",
        "Model calls per rolling week — the subscription's second ceiling. The manager sleeps "
        "the crew when it is close instead of hitting the wall mid-build."),
    "repair_headroom_min": (
        3, int, "REPAIR_HEADROOM_MIN",
        "Sessions of session-window headroom required to START a sprint; below "
        "it the manager sleeps until the window rolls. Starting with less risks "
        "dying between build and verify."),
    # --- utilization: is the shared quota actually idle? ---
    "usage_window_h": (
        5.0, float, "USAGE_WINDOW_H",
        "The rolling window every utilization number is measured over. 5 hours "
        "because that is the session window Claude subscriptions reset on — "
        "measuring over a different span would produce a meter that reads full "
        "when the real quota has already rolled, or empty just before a wall."),
    "usage_budget_tokens": (
        1_000_000, int, "USAGE_BUDGET_TOKENS",
        "Input+output tokens one window is treated as holding — the denominator "
        "for every percentage on the Improve screen. A subscription publishes no "
        "token budget, so this is a dial you set by observation, not a fact we can "
        "read: raise it if the crew sleeps while the plan clearly has room, lower "
        "it if it keeps meeting real rate limits (which override it anyway). Cache "
        "reads are counted separately and deliberately excluded — one build reads "
        "millions of them, and folding those in would make any budget meaningless."),
    "repair_idle_share": (
        0.6, float, "REPAIR_IDLE_SHARE",
        "The most of one window self-repair may take when the box is otherwise "
        "idle. Its actual allowance is this MINUS whatever the owner's own work "
        "already spent, so a busy day shrinks the crew automatically and a quiet "
        "one hands it the room — no separate crew budget to keep in sync. Not 1.0: "
        "the owner must be able to start work without waiting for a window to roll."),
    "repair_yield_quiet_s": (
        900, int, "REPAIR_YIELD_QUIET_S",
        "How long the platform must be free of owner-driven model spend before the "
        "crew claims idle quota. This is the difference between 'nobody is using "
        "it' and 'nobody used it in the last second': 15 minutes is short enough "
        "that the crew still gets nights and lunch breaks, long enough that it "
        "does not elbow into a session the owner is in the middle of."),
    "repair_builder_model": (
        "claude-sonnet-5", str, "REPAIR_BUILDER_MODEL",
        "The model that scouts and builds. Quality over price here: cheap-model "
        "fixes get reverted more often than they save, and a revert costs a "
        "whole sprint."),
    "repair_max_turns": (
        50, int, "REPAIR_MAX_TURNS",
        "Turn cap on one build session. A fix that needs more turns than this "
        "is a redesign wearing a fix's clothes — fail it and let the crew "
        "re-scope next sprint."),
    "repair_scout_max_turns": (
        40, int, "REPAIR_SCOUT_MAX_TURNS",
        "Turn cap on the read-only survey session. Scouting is browsing, not "
        "building; a tighter cap keeps its cost a fraction of a build."),
    "repair_fix_attempts": (
        1, int, "REPAIR_FIX_ATTEMPTS",
        "Red-suite retries per task (with the failure text as evidence) before "
        "the branch is abandoned. More retries mostly re-buy the same failure."),
    "repair_verify_timeout_s": (
        900, int, "REPAIR_VERIFY_TIMEOUT_S",
        "Ceiling on one full-suite verification run in a worktree. The suite "
        "takes ~1-2 minutes on a laptop; far past that it is hung, not slow."),
    "repair_sprint_pause_s": (
        300, int, "REPAIR_SPRINT_PAUSE_S",
        "Breath between sprints so the feed stays followable, the ledger "
        "smooths, and a runaway improvement loop cannot melt a laptop."),
    "agent_session_cap": (
        30, int, "AGENT_SESSION_CAP",
        "Model-uses one Studio agent may make inside its rolling session window "
        "before it sleeps. Promoted from env-only so a running instance can "
        "tune it like every other spend door."),
    "agent_session_window_s": (
        5 * 3600, int, "AGENT_SESSION_WINDOW_S",
        "The rolling window (seconds) behind agent_session_cap — Claude's "
        "subscription session is about five hours, so that is the default."),
}


def defaults() -> dict[str, Any]:
    return {k: v[0] for k, v in KNOBS.items()}


def _coerce(kind: type, raw: str) -> Any:
    if kind is bool:
        return raw.strip().lower() not in ("0", "false", "no", "")
    return kind(raw)


def _from_env() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, (_default, kind, env, _why) in KNOBS.items():
        raw = config._env(env)
        if raw != "":
            try:
                out[name] = _coerce(kind, raw)
            except (TypeError, ValueError):
                pass  # a malformed env var falls back to the default, never crashes boot
    return out


def all() -> dict[str, Any]:
    """Every knob's effective value."""
    values = defaults()
    values.update(_from_env())
    try:
        stored = db.tuning_all()
    except Exception:
        stored = {}  # before db.init(), fall back to defaults rather than failing
    for k, v in stored.items():
        if k in KNOBS:
            values[k] = v
    return values


def get(name: str) -> Any:
    return all().get(name, KNOBS[name][0] if name in KNOBS else None)


def set(name: str, value: Any, who: str = "") -> Any:
    """Change a knob on the running instance. Returns the coerced value.

    Unknown names are refused rather than stored: a typo that silently persists
    looks exactly like a knob that does not work.
    """
    if name not in KNOBS:
        raise KeyError(f"unknown knob '{name}'")
    kind = KNOBS[name][1]
    if kind is bool:
        value = bool(value)
    else:
        value = kind(value)
    db.tuning_set(name, value, who)
    return value


def reset(name: str) -> None:
    if name not in KNOBS:
        raise KeyError(f"unknown knob '{name}'")
    db.tuning_clear(name)


def describe() -> list[dict]:
    """Knobs with their provenance, for the settings UI: what it is, what it is
    now, where that value came from, and why the default is what it is."""
    env = _from_env()
    try:
        stored = db.tuning_all()
    except Exception:
        stored = {}
    out = []
    for name, (default, kind, envvar, why) in KNOBS.items():
        if name in stored:
            source = "tuned"
        elif name in env:
            source = "environment"
        else:
            source = "default"
        out.append({
            "name": name,
            "value": stored.get(name, env.get(name, default)),
            "default": default,
            "type": kind.__name__,
            "env": envvar,
            "source": source,
            "why": why,
        })
    return out


def profile_of(project: dict | None) -> str:
    """Which tuning profile a project's runs are stamped with, so runs made under
    different settings are comparable rather than pooled."""
    return (project or {}).get("profile") or "default"
