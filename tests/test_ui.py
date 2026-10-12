"""Smoke tests: real window, offscreen, fake trash, suggestion service down."""

import pytest
from conftest import tag_id
from PyQt6.QtCore import Qt

from flicktag.core import library
from flicktag.ui.window import MainWindow


@pytest.fixture
def window(qtbot, seeded, photos, fake_trash):
    seeded.set_setting("suggest_url", "http://127.0.0.1:9")  # nothing listens there
    lib = seeded.add_library(photos)
    library.scan(seeded, lib)
    win = MainWindow(seeded, lib.id, trash_fn=fake_trash.trash)
    win.history.restore_fn = fake_trash.restore
    qtbot.addWidget(win)
    win.show()
    qtbot.waitUntil(lambda: win.session.current_id in win._cache, timeout=5000)
    yield win
    win.workers.wait()


def test_keys_tag_validate_and_advance(qtbot, window, seeded):
    first = window.session.current_id
    standing = seeded.tag(tag_id(seeded, "pose", "standing"))
    qtbot.keyClick(window, standing.key)
    assert window.tagbar.chip_state(standing.id) == "on"
    qtbot.keyClick(window, Qt.Key.Key_Space)
    assert seeded.image_tag_ids(first) == {standing.id}
    assert window.session.current_id != first
    qtbot.waitUntil(lambda: not seeded.image(first).xmp_pending, timeout=5000)


def test_chip_click_toggles(qtbot, window, seeded):
    hand = tag_id(seeded, "subject", "hand")
    window.tagbar._chips[hand].click()
    assert hand in window.session.checked


def test_delete_then_undo(qtbot, window, seeded):
    first = window.session.current_id
    path = seeded.image_path(first)
    qtbot.keyClick(window, Qt.Key.Key_Delete)
    assert not path.exists()
    qtbot.keyClick(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert path.exists()
    assert window.session.current_id == first


def test_offline_indicator(qtbot, window):
    qtbot.waitUntil(lambda: window.suggest_online is False, timeout=10000)
    assert "offline" in window.header.text()


def test_tab_hides_tagbar(qtbot, window):
    assert window.tagbar.isVisible()
    qtbot.keyClick(window, Qt.Key.Key_Tab)
    assert not window.tagbar.isVisible()


def test_menubar_menus_fill(qtbot, window):
    menus = [action.menu() for action in window.menuBar().actions()]
    assert [m.title() for m in menus] == ["&File", "&Queue", "&Tools", "&Help"]
    for menu in menus:
        menu.aboutToShow.emit()
        assert menu.actions()
    queue = menus[1]
    queue.aboutToShow.emit()  # refilling must not duplicate entries
    assert [a.text() for a in queue.actions()].count("Untagged only") == 1


def test_enter_validates_like_space(qtbot, window, seeded):
    first = window.session.current_id
    hand = seeded.tag(tag_id(seeded, "subject", "hand"))
    qtbot.keyClick(window, hand.key)
    qtbot.keyClick(window, Qt.Key.Key_Return)
    assert seeded.image_tag_ids(first) == {hand.id}
    assert window.session.current_id != first


def test_shift_letter_toggles_shift_tag_only(qtbot, window, seeded):
    skeleton = seeded.tag(tag_id(seeded, "subject", "skeleton"))
    standing = seeded.tag(tag_id(seeded, "pose", "standing"))
    assert (skeleton.key, standing.key) == ("S", "s")
    qtbot.keyClick(window, "s", Qt.KeyboardModifier.ShiftModifier)
    assert skeleton.id in window.session.checked
    assert standing.id not in window.session.checked


def test_tidy_dialog_previews_applies_and_saves_layout(qtbot, window, seeded):
    from flicktag.ui.dialogs import OrganizeDialog

    lib_id = window.session.library_id
    dialog = OrganizeDialog(seeded, lib_id, tidy=True, parent=window)
    qtbot.addWidget(dialog)
    assert dialog.table.rowCount() == 3  # every image is untagged -> À trier
    dialog._apply()
    assert seeded.library(lib_id).layout == dialog.template.text()
    assert all(i.rel_path.startswith("À trier/") for i in seeded.images(lib_id))


def test_rescan_tidies_a_library_with_a_layout(qtbot, window, seeded):
    lib = seeded.library(window.session.library_id)
    seeded.set_layout(lib.id, "{subject}/{subject}_{uid}", "inbox")
    window.open_library(lib.id)
    qtbot.waitUntil(lambda: "Tidy: 3 moved" in window.toast.text(), timeout=5000)
    assert all(i.rel_path.startswith("inbox/") for i in seeded.images(lib.id))
    qtbot.waitUntil(lambda: window.session.current_id in window._cache, timeout=5000)
