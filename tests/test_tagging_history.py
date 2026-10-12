import os
import time
from pathlib import Path

import pytest
from conftest import make_image, tag_id

from flicktag.core import library, trashcan
from flicktag.core.history import History, HistoryError
from flicktag.core.tagging import validate


@pytest.fixture
def lib(seeded, photos):
    lib = seeded.add_library(photos)
    library.scan(seeded, lib)
    return lib


def feedback(store, image_id):
    return {r["tag_id"]: r["label"] for r in store.query("SELECT * FROM feedback WHERE image_id = ?", (image_id,))}


def test_validate_records_tags_sources_and_feedback(seeded, lib):
    image = seeded.images(lib.id)[0]
    standing = tag_id(seeded, "pose", "standing")
    figure = tag_id(seeded, "subject", "figure")
    hand = tag_id(seeded, "subject", "hand")
    validate(seeded, image.id, chosen={standing, figure}, suggested={standing, hand})
    assert seeded.image_tag_ids(image.id) == {standing, figure}
    sources = {r["tag_id"]: r["source"] for r in seeded.query("SELECT * FROM image_tags")}
    assert sources == {standing: "suggested", figure: "manual"}
    assert feedback(seeded, image.id) == {standing: 1, figure: 1, hand: 0}
    assert seeded.image(image.id).status == "tagged"


def test_undo_validation_restores_previous_state(seeded, lib, fake_trash):
    history = History(seeded, fake_trash.trash, fake_trash.restore)
    image = seeded.images(lib.id)[0]
    standing = tag_id(seeded, "pose", "standing")
    sitting = tag_id(seeded, "pose", "sitting")
    history.validate(image.id, {standing}, set())
    history.validate(image.id, {sitting}, {standing})
    assert history.undo() == image.id
    assert seeded.image_tag_ids(image.id) == {standing}
    assert feedback(seeded, image.id) == {standing: 1}
    assert history.undo() == image.id
    assert seeded.image_tag_ids(image.id) == set()
    assert seeded.image(image.id).status == "untagged"
    assert feedback(seeded, image.id) == {}
    assert history.undo() is None


def test_trash_and_undo(seeded, lib, fake_trash):
    history = History(seeded, fake_trash.trash, fake_trash.restore)
    image = seeded.images(lib.id)[0]
    path = seeded.image_path(image)
    history.trash(image.id)
    assert not path.exists()
    assert seeded.image(image.id).status == "trashed"
    history.undo()
    assert path.exists()
    assert seeded.image(image.id).status == "untagged"


def test_trash_failure_changes_nothing(seeded, lib, fake_trash):
    history = History(seeded, fake_trash.trash, fake_trash.restore)
    image = seeded.images(lib.id)[0]
    fake_trash.fail = True
    with pytest.raises(HistoryError):
        history.trash(image.id)
    assert seeded.image(image.id).status == "untagged"
    assert len(history) == 0


def test_freedesktop_restore(tmp_path, monkeypatch):
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    trash = data_home / "Trash"
    (trash / "files").mkdir(parents=True)
    (trash / "info").mkdir()
    original = make_image(tmp_path / "dir with space" / "été.jpg")
    # Simulate what a file manager does, twice, keeping the newest.
    for n, stamp in enumerate(["2026-01-01T10:00:00", "2026-02-01T10:00:00"]):
        stored = trash / "files" / f"été.{n}.jpg"
        make_image(stored, color=(n * 100, 0, 0))
        from urllib.parse import quote

        (trash / "info" / f"été.{n}.jpg.trashinfo").write_text(
            f"[Trash Info]\nPath={quote(str(original))}\nDeletionDate={stamp}\n", encoding="utf-8"
        )
    newest = (trash / "files" / "été.1.jpg").read_bytes()
    original.unlink()
    assert trashcan.restore(original)
    assert original.read_bytes() == newest
    assert not (trash / "info" / "été.1.jpg.trashinfo").exists()
    assert (trash / "files" / "été.0.jpg").exists()


def test_restore_refuses_to_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    existing = make_image(tmp_path / "x.jpg")
    assert not trashcan.restore(existing)
