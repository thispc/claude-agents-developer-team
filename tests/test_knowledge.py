"""The knowledge base: what an agent learned, stored so it can be found again.

Retrieval is the whole product claim here — an agent that gets better with experience is one
that can FIND its experience. So these test recall quality on paraphrases, not just plumbing.
"""

import asyncio

import pytest

from app import knowledge


def _seed():
    async def go():
        knowledge.forget("a1")
        await knowledge.remember("a1", "the build failed with ImportError: no module named app",
                                 "an ImportError here means the venv symlink, not the code",
                                 sig="error:ImportError", good=2)
        await knowledge.remember("a1", "HTTP 505 from the billing host on staging",
                                 "a 505 there is the staging env, not the API",
                                 sig="http:505", good=3)
        await knowledge.remember("a1", "the worker timed out connecting to 127.0.0.1:8787",
                                 "a connection timeout locally means the server is not up yet",
                                 good=1)
        await knowledge.remember("a1", "the venv symlink was missing from the new worktree",
                                 "symlink .venv into every worktree or nothing imports", good=2)
    asyncio.run(go())


def test_the_cue_is_the_situation_not_the_lesson(fresh_db):
    """Embedding the lesson is the classic mistake: you then retrieve by similarity to
    ANSWERS, and an agent that already knew the answer would not be asking."""
    _seed()
    hits = asyncio.run(knowledge.recall("a1", "venv symlink missing", k=1))
    assert hits and "symlink .venv into every worktree" in hits[0]["says"]
    # asking with the words of the LESSON must not be the only way to find it
    hits2 = asyncio.run(knowledge.recall("a1", "fresh worktree cannot import app", k=2))
    assert hits2, "a situation described in its own words found nothing"


def test_it_finds_a_situation_described_differently(fresh_db):
    _seed()
    hits = asyncio.run(knowledge.recall("a1", "nothing responds on localhost port 8787", k=1))
    assert hits and "server is not up" in hits[0]["says"]
    assert "8787" in hits[0]["why"]["matched"], "it should say which rare term earned it"


def test_it_stays_silent_on_something_it_knows_nothing_about(fresh_db):
    """A knowledge base that always answers is one that cannot be trusted when it does."""
    _seed()
    assert asyncio.run(knowledge.recall("a1", "what is the capital of France")) == []


def test_relevance_decides_and_a_good_record_only_breaks_ties(fresh_db):
    """Adding a track record and a recency bonus as TERMS let a lesson that has always worked
    outrank one that is actually about the question — every row scored the same and the
    ordering was noise."""
    _seed()
    hits = asyncio.run(knowledge.recall("a1", "we got a 505 talking to billing", k=3))
    assert hits[0]["sig"] == "http:505"
    assert hits[0]["why"]["relevance"] > 0.2
    # a prior can only be a multiplier near 1, so it can never carry an irrelevant row
    assert all(h["why"]["relevance"] >= knowledge.FLOOR for h in hits)


def test_stopwords_do_not_count_as_a_match(fresh_db):
    """IDF alone cannot separate "on" from "8787" until the corpus is large — and a knowledge
    base is smallest exactly when it is newest."""
    assert knowledge._tokens("the build is on the host") == ["build", "host"]
    # Single digits and two-digit numbers are the debris of tokenising an IP or a version;
    # 127 and 8787 are as rare and as identifying as 505, so they stay.
    assert knowledge._tokens("127.0.0.1:8787") == ["127", "8787"]
    assert knowledge._tokens("v1.2 of the app") == ["v1", "app"]


def test_every_hit_explains_itself(fresh_db):
    """On a blended score the reason is genuinely not obvious: a hit can win on wording, on a
    shared rare term, or on having been right ten times before."""
    _seed()
    h = asyncio.run(knowledge.recall("a1", "505 from billing", k=1))[0]
    assert set(h["why"]) >= {"similarity", "shared_terms", "relevance", "held_up", "matched"}


def test_a_vector_is_never_compared_across_backends(fresh_db):
    """A hashed vector against a neural one is not a worse answer, it is a meaningless one."""
    import inspect
    src = inspect.getsource(knowledge.recall)
    assert 'r["backend"] == backend' in src
    assert "embed_local(r[\"cue\"])" in src, "a mismatched row must be re-embedded, not skipped"


def test_reinforcement_is_the_only_thing_that_moves_confidence(fresh_db):
    _seed()
    hit = asyncio.run(knowledge.recall("a1", "505 from billing", k=1))[0]
    before = hit["confidence"]
    knowledge.reinforce(hit["id"], "bad")
    after = asyncio.run(knowledge.recall("a1", "505 from billing", k=1))[0]["confidence"]
    assert after < before


def test_it_prunes_what_never_helped(fresh_db, monkeypatch):
    """A knowledge base that only grows is a landfill."""
    monkeypatch.setattr(knowledge, "MAX_PER_OWNER", 5)

    async def go():
        for i in range(12):
            await knowledge.remember("a2", f"a thing that happened number {i}", f"lesson {i}")
    asyncio.run(go())
    assert knowledge.stats("a2")["total"] <= 5


def test_an_agent_consults_it_before_thinking():
    """The point of remembering is not having to think again — so the lookup happens BEFORE
    the model call, not as a post-hoc annotation."""
    from pathlib import Path
    from app.lifeworld import human as hmod
    src = Path(hmod.__file__).read_text()
    body = src.split("async def perceive", 1)[1].split("async def _recall_similar", 1)[0]
    assert "_recall_similar" in body and body.index("_recall_similar") < body.index("world.appraise")
    assert '"via": "similar"' in src
