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
