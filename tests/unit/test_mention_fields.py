"""What a target's mention counts and free-text answers say about adoption."""

from __future__ import annotations

from experiments.measures import mention_fields

PATTERN = r"\bkestrel\b"


def counted(own_mentions):
    return {"exposure": 0, "mentions": own_mentions or 0, "own_mentions": own_mentions,
            "copied_posts": 0, "copied": [], "mention_channels": {}}


def test_an_answer_that_uses_the_name_outside_recall_is_spontaneous():
    fields = mention_fields(PATTERN, counted(0), {"handover": "Call it Kestrel."})
    assert fields["free_probes"]["handover"]["uses"] is True
    assert fields["spontaneous"] is True


def test_a_recall_answer_uses_the_name_but_is_not_spontaneous():
    fields = mention_fields(PATTERN, counted(0), {"recall": "The name was kestrel."})
    assert fields["free_probes"]["recall"]["uses"] is True
    assert fields["spontaneous"] is False


def test_own_mentions_make_it_spontaneous_without_any_answers():
    assert mention_fields(PATTERN, counted(2), {})["spontaneous"] is True


def test_unknown_own_mentions_leave_spontaneous_unknown():
    assert mention_fields(PATTERN, counted(None), {})["spontaneous"] is None
