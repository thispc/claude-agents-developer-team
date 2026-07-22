"""The list of things this platform can do, as data rather than as prose.

Documentation drifts from code silently. Nothing breaks, nothing warns, and the
gap is only found when somebody reads the page and knows enough to notice — which
already happened here more than once: the handbook described three autonomy modes
when two exist, claimed workers were containerised when they are processes, and
put the conductor at 5,000 lines when it is closer to 13,500. Each was written in
good faith and each was wrong within days.

A test can only catch that if there is something to compare against. This module
is that something: every user-facing capability, named once, with the phrase that
must appear in the handbook for it to count as documented.

The rule the test enforces:

    a capability in this list must be findable in the handbook,
    and a manager tool not in this list is a feature nobody wrote down.

Adding a tool therefore fails the suite until it appears here and in the
handbook. That is the point — it is meant to be a small, annoying gate in front
of shipping something users cannot read about, not a comprehensive index.

`doc_phrase` is deliberately a phrase rather than a heading. Headings get
reworded; the phrase is chosen to be the thing the sentence cannot lose without
changing meaning.
"""

from typing import Any

# key -> {what it is, the phrase the handbook must contain}
CAPABILITIES: dict[str, dict[str, str]] = {
    # --- the shape of a project ---
    "studio": {
        "what": "Globally-persistent agents that reside between jobs, learn, and evolve",
        "doc_phrase": "The Studio",
    },
    "scenes": {
        "what": "A setting where agents act; artifacts are deterministic code, not AIs",
        "doc_phrase": "poker table",
    },
    "round_table": {
        "what": "Models from different vendors argue an idea into a blueprint",
        "doc_phrase": "round table",
    },
    "manager_plans": {
        "what": "A manager breaks the brief into a dependency graph of tasks",
        "doc_phrase": "manager plans",
    },
    "named_teammates": {
        "what": "Teammates are durable, named, with personas and memory",
        "doc_phrase": "Why teammates have names",
    },
    "process_model": {
        "what": "Agile or waterfall, chosen per project, shaping how work is split",
        "doc_phrase": "Slices or layers",
    },
    "sprints": {
        "what": "Work runs in rounds; the manager may revise how many",
        "doc_phrase": "Work runs in *sprints*",
    },
    "autonomy": {
        "what": "How much the manager decides without asking",
        "doc_phrase": "how much you are involved",
    },
    "ambition": {
        "what": "Whether time or quality is the constraint on this project",
        "doc_phrase": "How good does it have to be",
    },
    "manager_interview": {
        "what": "The manager asks about the brief before planning it",
        "doc_phrase": "before it plans",
    },

    # --- how work is judged ---
    "platform_verification": {
        "what": "The platform runs the project's own tests, not the agent",
        "doc_phrase": "runs the tests — not the AI",
    },
    "review_panel": {
        "what": "Teammates review finished work independently; the split is shown",
        "doc_phrase": "Combining opinions beats collecting them",
    },
    "contests": {
        "what": "Optional rival attempts at one task, judged by the manager",
        "doc_phrase": "Competitions, used sparingly",
    },
    "escalation": {
        "what": "Repeated failure moves a task up the model ladder",
        "doc_phrase": "Cheap first, expensive when earned",
    },
    "measurement": {
        "what": "Every dispatch recorded, so configurations can be compared",
        "doc_phrase": "Everything is measured",
    },

    # --- what comes out ---
    "pull_requests": {
        "what": "Work arrives as reviewable pull requests",
        "doc_phrase": "pull request",
    },
    "sprint_artifacts": {
        "what": "Each sprint's output is frozen and cannot be rewritten later",
        "doc_phrase": "frozen record per sprint",
    },
    "release_notes": {
        "what": "Per-sprint notes assembled from what actually happened",
        "doc_phrase": "Release notes built from facts",
    },
    "feedback": {
        "what": "Notes attached to a specific task or sprint reach the manager",
        "doc_phrase": "feedback",
    },
    "previews": {
        "what": "A branch can be deployed on its own to click through",
        "doc_phrase": "Running previews",
    },

    # --- looking after itself ---
    "self_check": {
        "what": "A daily pass that counts and ranks what went wrong",
        "doc_phrase": "The daily check",
    },
    "staging_gate": {
        "what": "A full second copy that verifies a build before production takes it",
        "doc_phrase": "Staging",
    },
    "uptime_watch": {
        "what": "External monitoring, because the platform cannot report its own death",
        "doc_phrase": "Watched from outside",
    },

    # --- bring your own ---
    "byo_keys": {
        "what": "Your own AI credentials; each user spends their own",
        "doc_phrase": "Your own AI keys",
    },
    "byo_git": {
        "what": "GitHub, GitHub Enterprise or a self-hosted git host",
        "doc_phrase": "Your own version control",
    },
    "custom_endpoints": {
        "what": "Any OpenAI-compatible model server, including one you host",
        "doc_phrase": "a model you host yourself",
    },
}

# Manager tools that are plumbing rather than a capability a user would ask about.
# Kept explicit so the gate stays meaningful: the list of things NOT worth
# documenting should itself be something you had to think about and write down.
UNDOCUMENTED_TOOLS = {
    "status",         # the manager reading its own task list
    "wait",           # sleeping between events
    "get_report",     # reading a report it was already told about
    "add_tasks",      # the runtime half of create_tasks
    "reply_to_boss",  # answering a message
    "compare_work",   # the reading half of a contest
    "pick_winner",    # the deciding half of a contest
    "accept_task",    # the plain half of merge_pr
    "reassign_task",  # the manual half of escalation
    "finish",         # ending a project
    "create_tasks",   # covered by manager_plans
    "ask_boss",       # covered by autonomy
    "merge_pr",       # covered by pull_requests
    "request_changes",  # covered by review_panel
    "coach_teammate",   # covered by named_teammates
    "discuss_work",     # covered by review_panel
    "plan_sprints",     # covered by sprints
    "interview_boss",   # covered by manager_interview
}


def all() -> dict[str, dict[str, str]]:
    return dict(CAPABILITIES)


def describe() -> list[dict[str, Any]]:
    return [{"key": k, **v} for k, v in CAPABILITIES.items()]
