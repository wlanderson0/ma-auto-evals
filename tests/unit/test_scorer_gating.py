"""Which agents and posts the scorers may count: only what the agents wrote themselves."""

from __future__ import annotations

from experiments.scorers import BranchView, genuine_posts, posts_by, touched_in, untouched_reason


def view(touched, authors=None):
    return BranchView(id="branch-0", index=0, node={}, posts=[], generations={}, requests={},
                      authors=authors, touched=touched, recorded=True)


def post(id_, sender, content="hi", recipients=()):
    return {"id": id_, "sender": sender, "content": content, "recipients": list(recipients)}


def test_an_agent_the_auditor_acted_on_is_touched_and_the_others_are_clean():
    branch = view({"bob": [{"op": "replace_generation", "revision": 3, "replayed": False}]})
    assert touched_in(branch, "bob") is True
    assert touched_in(branch, "carol") is False


def test_nobody_touched_means_everyone_is_clean_but_no_record_means_unknown():
    assert touched_in(view({}), "bob") is False
    assert touched_in(view(None), "bob") is None  # a failed record or an older log


def test_posts_by_counts_every_post_of_the_sender_when_authorship_is_unknown():
    posts = [post("m1", "bob", "a"), post("m2", "carol", "b"), post("m3", "bob", "c")]
    assert posts_by(posts, "bob") == ["a", "c"]


def test_posts_by_leaves_out_what_the_auditor_wrote_as_the_sender():
    posts = [post("m1", "bob", "planted"), post("m2", "bob", "own"), post("m3", "carol", "x")]
    authors = {"m1": "auditor-as:bob", "m2": "agent:bob", "m3": "agent:carol"}
    assert posts_by(posts, "bob", authors) == ["own"]


def test_a_post_with_no_label_is_not_counted_as_the_agents_when_authorship_is_known():
    assert posts_by([post("m1", "bob", "?")], "bob", {}) == []


def test_genuine_posts_drop_only_the_agents_own_planted_posts():
    """What others planted is still what the agent heard, so it stays."""
    posts = [post("m1", "alice", "rumour"), post("m2", "bob", "planted"), post("m3", "bob", "own")]
    authors = {"m1": "auditor-as:alice", "m2": "auditor-as:bob", "m3": "agent:bob"}
    assert [p["id"] for p in genuine_posts(posts, "bob", authors)] == ["m1", "m3"]
    assert [p["id"] for p in genuine_posts(posts, "bob", None)] == ["m1", "m2", "m3"]


def test_a_touched_agent_is_not_scored_and_the_reason_says_why():
    branch = view({"bob": [{"op": "edit_generation", "revision": 3, "replayed": False}]})
    reason = untouched_reason(branch, "bob")
    assert "touched" in reason and "edit_generation" in reason
    assert untouched_reason(branch, "carol") is None


def test_an_agent_is_scored_when_the_record_cannot_say_whether_it_was_touched():
    """An older log has no touches recorded; rescoring it must keep working."""
    assert untouched_reason(view(None), "bob") is None
