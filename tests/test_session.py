import pytest
from conftest import tag_id

from flicktag.core import library
from flicktag.core.history import History
from flicktag.core.session import Session
from flicktag.core.queue import QueueFilter
from flicktag.suggest.engine import preselect


@pytest.fixture
def lib(seeded, photos):
    lib = seeded.add_library(photos)
    library.scan(seeded, lib)
    return lib


@pytest.fixture
def session(seeded, lib, fake_trash):
    history = History(seeded, fake_trash.trash, fake_trash.restore)
    return Session(seeded, lib.id, history, lambda scores: preselect(seeded, scores, 0.5))


def test_suggestions_are_prechecked_and_keyed_off(seeded, session):
    standing = tag_id(seeded, "pose", "standing")
    hand = tag_id(seeded, "subject", "hand")
    session.set_scores(session.current_id, {standing: 0.9, hand: 0.7})
    assert session.tag_state(standing) == "suggested"
    session.toggle(hand)  # reject a suggestion
    assert session.tag_state(hand) == "off"
    first = session.current_id
    session.validate_and_next()
    assert seeded.image_tag_ids(first) == {standing}
    labels = {r["tag_id"]: r["label"] for r in seeded.query("SELECT * FROM feedback WHERE image_id = ?", (first,))}
    assert labels == {standing: 1, hand: 0}
    assert session.current_id != first


def test_exclusive_category_replaces(seeded, session):
    standing = tag_id(seeded, "pose", "standing")
    sitting = tag_id(seeded, "pose", "sitting")
    session.toggle(standing)
    session.toggle(sitting)
    assert session.checked == {sitting}


def test_late_scores_do_not_override_user_edits(seeded, session):
    standing = tag_id(seeded, "pose", "standing")
    hand = tag_id(seeded, "subject", "hand")
    session.toggle(hand)
    session.set_scores(session.current_id, {standing: 0.99})
    assert session.checked == {hand}


def test_scores_for_upcoming_image_apply_on_arrival(seeded, session):
    standing = tag_id(seeded, "pose", "standing")
    upcoming = session.upcoming(2)[1]
    session.set_scores(upcoming, {standing: 0.9})
    session.next()
    assert session.current_id == upcoming
    assert session.tag_state(standing) == "suggested"


def test_tagged_images_are_not_prechecked(seeded, session):
    standing = tag_id(seeded, "pose", "standing")
    sitting = tag_id(seeded, "pose", "sitting")
    first = session.current_id
    session.toggle(sitting)
    session.validate_and_next()
    session.prev()
    assert session.current_id == first
    session.set_scores(first, {standing: 0.99})
    assert session.checked == {sitting}


def test_trash_skips_and_undo_returns(seeded, session):
    first = session.current_id
    session.trash()
    assert session.current_id != first
    session.prev()
    assert session.current_id != first  # trashed images are skipped
    assert session.undo() == first
    assert session.current_id == first and seeded.image(first).status == "untagged"


def test_undo_validation_goes_back(seeded, session):
    validated = []
    session.on_validated = validated.append
    first = session.current_id
    session.toggle(tag_id(seeded, "subject", "hand"))
    session.validate_and_next()
    session.undo()
    assert session.current_id == first
    assert seeded.image_tag_ids(first) == set()
    assert validated == [first, first]  # XMP resynced after the undo too


def test_end_of_queue(session):
    for _ in range(10):
        session.validate_and_next()
    assert session.finished
    session.validate_and_next()  # no-op, no crash
    assert session.prev()
    assert not session.finished


def test_resume_at_last_untagged(seeded, lib, fake_trash):
    history = History(seeded, fake_trash.trash, fake_trash.restore)
    first = Session(seeded, lib.id, history, lambda s: set())
    first.next()
    second_id = first.current_id
    again = Session(seeded, lib.id, history, lambda s: set())
    assert again.current_id == second_id


def test_tag_filter(seeded, lib, session):
    hand = tag_id(seeded, "subject", "hand")
    target = session.current_id
    session.toggle(hand)
    session.validate_and_next()
    review = Session(seeded, lib.id, session.history, lambda s: set(), QueueFilter("tag", hand))
    assert review.queue == [target]


def test_refresh_queue_drops_deleted_tags(seeded, session):
    from flicktag.core import vocab

    hand = tag_id(seeded, "subject", "hand")
    session.toggle(hand)
    vocab.delete_tag(seeded, hand)
    session.refresh_queue()
    assert hand not in session.checked
