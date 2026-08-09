"""Artifacts are CODE, not agents — and this is where that code lives.

An artifact (a card, a deck, a pot) is a piece of state an agent interacts with,
whose *effect* is a pure deterministic function named on the registry at the bottom
of this file. The owner's whole idea is here: seeing a card "unravels something" —
it is added to a private hand — and when enough cards are seen they *collate*, the
cards' own code combining into a single output (a poker hand rank) the agent then
merely speaks about. The collation is code. The speaking, in `scene.py`, is the one
place a token is ever spent.

Two disciplines are load-bearing, both the owner's own constraints turned into
architecture:

- **Effects are free.** Dealing, flipping, adding to a hand, computing a hand rank,
  deciding who may see what — deterministic code, zero model calls. Nothing in this
  module imports `providers`; there is no way to spend a token from here.

- **No arbitrary code, ever.** An artifact runs one of a small set of *typed*
  effects the platform ships, each a reviewed function selected by `type` — never
  `exec` of a string a scene contains. "Later, a custom artifact compiled by us"
  becomes "add a reviewed effect type," which keeps the analogy's power without
  opening an RCE in a multi-tenant system — the door to keep shut after the
  credential leak. `apply()` is the only dispatch and it raises on anything not on
  the table, so a typo is a refusal, not a silent no-op.

The name is `effects`, not `artifacts`, because `artifacts.py` already means the
frozen record of what a sprint shipped — a different noun entirely.
"""

import random
from typing import Any

# A card is (rank, suit). Suits as letters, not glyphs, so a card survives a JSON
# round-trip and a terminal without an encoding argument.
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
SUITS = ["s", "h", "d", "c"]
_RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}  # 2..14, ace high

# Hand categories, worst to best. The evaluator returns one of these plus tiebreak
# ranks, so comparing two hands is a plain tuple comparison — the winner is decided
# by this arithmetic, never by a model's say-so.
CATEGORIES = [
    "high card", "pair", "two pair", "three of a kind", "straight",
    "flush", "full house", "four of a kind", "straight flush",
]


# --- the deck: a reviewed effect, deterministic given a seed ------------------

def fresh_deck(seed: int = 0) -> dict:
    """A full 52-card deck, shuffled deterministically. The same seed deals the same
    hand, which is what lets the acceptance test reproduce a match exactly and assert
    the winner with no model in the loop."""
    cards = [{"rank": r, "suit": s} for s in SUITS for r in RANKS]
    random.Random(seed).shuffle(cards)
    return {"cards": cards, "cursor": 0}


def draw(deck_state: dict, n: int = 1) -> tuple[list[dict], dict]:
    """Take the top `n` cards off the deck. Returns (cards, advanced deck). Pure —
    the deck is not mutated in place; the caller stores the returned state, so a
    replay from the same stored state deals the same cards."""
    cur = int(deck_state.get("cursor", 0))
    cards = deck_state.get("cards", [])
    taken = cards[cur:cur + n]
    return taken, {**deck_state, "cursor": cur + len(taken)}


# --- the card: seeing it adds it to a hand; enough of them collate to a rank ---

def see(hand: list[dict], card: dict) -> list[dict]:
    """The card's effect when an agent sees it: added to that agent's hand. The
    owner's line, literally — a seen card 'unravels' into what the holder knows.
    Idempotent on identical cards so a re-deal cannot duplicate one."""
    if any(c["rank"] == card["rank"] and c["suit"] == card["suit"] for c in hand):
        return hand
    return [*hand, card]


def back() -> dict:
    """What a face-down card shows to everyone but its holder: a back, never a value.
    The whole 'an agent can hide what it does not want revealed' property, rendered."""
    return {"facedown": True}


def collate(cards: list[dict]) -> dict:
    """Combine seen cards into ONE output: the best five-card poker hand they make.

    The owner's "five seen cards combine and collate to a single output," generalised
    to the seven a hold'em player sees (two private, five public): score every
    five-card subset, keep the best. Deterministic, free, code. The returned `score`
    is directly comparable — a larger tuple is a better hand."""
    best: tuple | None = None
    best_name = ""
    picked: list[dict] = []
    pool = [c for c in cards if c and "rank" in c and "suit" in c]
    for combo in _combinations(pool, 5):
        score, name = _score_five(combo)
        if best is None or score > best:
            best, best_name, picked = score, name, list(combo)
    if best is None:
        return {"category": "high card", "name": "nothing", "score": [0], "cards": []}
    return {"category": best_name, "name": best_name, "score": list(best), "cards": picked}


def beats(a: dict, b: dict) -> int:
    """-1, 0, 1 comparing two collated hands. The showdown's whole verdict, in code."""
    sa, sb = tuple(a.get("score", ())), tuple(b.get("score", ()))
    return (sa > sb) - (sa < sb)


# --- the pot: a reviewed effect over chips, deterministic ---------------------

def pot_add(pot_state: dict, chips: int) -> dict:
    return {**pot_state, "chips": int(pot_state.get("chips", 0)) + max(0, int(chips))}


def pot_take(pot_state: dict) -> tuple[int, dict]:
    won = int(pot_state.get("chips", 0))
    return won, {**pot_state, "chips": 0}


# --- the five-card evaluator (pure) -------------------------------------------

def _score_five(cards: list[dict]) -> tuple[tuple, str]:
    """Score exactly five cards → (comparable tuple, category name). The tuple is
    (category_index, *tiebreak ranks high-to-low), so a plain tuple comparison
    orders any two hands correctly."""
    vals = sorted((_RANK_VALUE[c["rank"]] for c in cards), reverse=True)
    suits = [c["suit"] for c in cards]
    is_flush = len(set(suits)) == 1

    distinct = sorted(set(vals), reverse=True)
    straight_high = 0
    if len(distinct) == 5:
        if distinct[0] - distinct[4] == 4:
            straight_high = distinct[0]
        elif distinct == [14, 5, 4, 3, 2]:   # the wheel: ace plays low
            straight_high = 5

    counts: dict[int, int] = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    by_count = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    shape = [c for _, c in by_count]
    ordered_ranks = [r for r, _ in by_count]

    cat = CATEGORIES.index
    if straight_high and is_flush:
        return (cat("straight flush"), straight_high), "straight flush"
    if shape == [4, 1]:
        return (cat("four of a kind"), *ordered_ranks), "four of a kind"
    if shape == [3, 2]:
        return (cat("full house"), *ordered_ranks), "full house"
    if is_flush:
        return (cat("flush"), *vals), "flush"
    if straight_high:
        return (cat("straight"), straight_high), "straight"
    if shape == [3, 1, 1]:
        return (cat("three of a kind"), *ordered_ranks), "three of a kind"
    if shape == [2, 2, 1]:
        return (cat("two pair"), *ordered_ranks), "two pair"
    if shape == [2, 1, 1, 1]:
        return (cat("pair"), *ordered_ranks), "pair"
    return (cat("high card"), *vals), "high card"


def _combinations(items: list, k: int):
    """Small hand-rolled combinations so this module has no import surprises. C(7,5)
    is 21 — this is never hot."""
    n = len(items)
    if k > n:
        return
    idx = list(range(k))
    yield [items[i] for i in idx]
    while True:
        for i in reversed(range(k)):
            if idx[i] != i + n - k:
                break
        else:
            return
        idx[i] += 1
        for j in range(i + 1, k):
            idx[j] = idx[j - 1] + 1
        yield [items[i] for i in idx]


# --- the typed registry: the ONLY way an artifact effect runs -----------------
#
# Every effect an artifact can have is named here, mapped to a reviewed function.
# There is no other dispatch and no `exec` anywhere in the module, so a scene can
# only run code that appears on this table. Adding a capability means adding a
# reviewed entry — the gate `capabilities.py` puts in front of shipping, applied to
# code an agent touches.
EFFECTS: dict[str, dict[str, Any]] = {
    "deck": {"fresh": fresh_deck, "draw": draw},
    "card": {"see": see, "back": back, "collate": collate, "beats": beats},
    "pot": {"add": pot_add, "take": pot_take},
}


def apply(atype: str, op: str, *args: Any, **kwargs: Any) -> Any:
    """Run one reviewed effect. Raises on an unknown type or op rather than doing
    nothing quietly — a typo that silently no-ops looks exactly like an effect that
    does not work, and here it would also be the seam an attacker probes."""
    fns = EFFECTS.get(atype)
    if fns is None:
        raise KeyError(f"no such artifact type {atype!r}")
    fn = fns.get(op)
    if fn is None:
        raise KeyError(f"artifact {atype!r} has no effect {op!r}")
    return fn(*args, **kwargs)
