import os

import pytest
from conftest import make_image, tag_id

from flicktag.core import layout, library, renamer, xmp
from flicktag.core.store import LibraryError
from flicktag.core.tagging import validate

TEMPLATE = "{subject}/{pose}/{subject}_{pose}_{uid}"


@pytest.fixture
def refs(seeded, tmp_path):
    """A messy reference folder: nested packs, one image already in a 'right-looking' folder."""
    root = tmp_path / "refs"
    make_image(root / "nu" / "assis" / "jpg" / "one.jpg", (10, 0, 0))
    make_image(root / "pack" / "two.jpg", (0, 10, 0))
    make_image(root / "figure" / "standing" / "three.jpg", (0, 0, 10))
    lib = seeded.add_library(root)
    library.scan(seeded, lib)
    return lib


def image(store, lib, name):
    return next(i for i in store.images(lib.id) if i.rel_path.endswith(name))


def tag(store, img, *pairs):
    validate(store, img.id, {tag_id(store, c, t) for c, t in pairs}, set())


# ------------------------------------------------------------------ layout


def test_default_template_uses_rename_ranks(seeded):
    assert layout.default_template(seeded) == TEMPLATE


def test_tagged_path_skips_empty_folders(seeded):
    segments = layout.parse_layout(TEMPLATE, {c.slug for c in seeded.categories()})
    assert layout.tagged_path(segments, {"subject": "figure", "pose": "sitting"}, "u1", "x/a.JPG") == (
        "figure/sitting/figure_sitting_u1.jpg"
    )
    assert layout.tagged_path(segments, {"subject": "hand"}, "u1", "x/a.jpg") == "hand/hand_u1.jpg"
    assert layout.tagged_path(segments, {}, "u1", "x/a.jpg") == "u1.jpg"


def test_template_without_slash_renames_in_place(seeded):
    segments = layout.parse_layout("{subject}_{uid}", {c.slug for c in seeded.categories()})
    assert layout.tagged_path(segments, {"subject": "hand"}, "u1", "deep/dir/a.jpg") == "deep/dir/hand_u1.jpg"


@pytest.mark.parametrize("template", ["{subject}/{pose}", "{subject}//{uid}", "/{uid}", "{nope}/{uid}"])
def test_bad_templates_rejected(seeded, template):
    with pytest.raises(layout.TemplateError):
        layout.parse_layout(template, {c.slug for c in seeded.categories()})


@pytest.mark.parametrize("text, ok", [("À trier", "À trier"), ("/inbox/", None), ("a/../b", None), ("  ", None)])
def test_untagged_dir_must_stay_inside(text, ok):
    if ok:
        assert layout.normalize_untagged_dir(text) == ok
    else:
        with pytest.raises(layout.TemplateError):
            layout.normalize_untagged_dir(text)


# ----------------------------------------------------------------- preview


def test_preview_sends_untagged_to_the_untagged_folder_even_from_a_right_looking_folder(seeded, refs):
    tag(seeded, image(seeded, refs, "one.jpg"), ("subject", "figure"), ("pose", "sitting"))
    ops = {op.old.name: op for op in renamer.preview(seeded, refs.id, TEMPLATE)}
    one = image(seeded, refs, "one.jpg")
    assert ops["one.jpg"].new == refs.root / f"figure/sitting/figure_sitting_{one.uid}.jpg"
    assert ops["two.jpg"].new == refs.root / "À trier/two.jpg"
    assert ops["three.jpg"].new == refs.root / "À trier/three.jpg"
    assert all(op.ok for op in ops.values())


def test_untagged_name_collision_gets_the_uid(seeded, tmp_path):
    root = tmp_path / "dup"
    make_image(root / "À trier" / "same.jpg", (1, 1, 1))
    make_image(root / "other" / "same.jpg", (2, 2, 2))
    lib = seeded.add_library(root)
    library.scan(seeded, lib)
    ops = renamer.preview(seeded, lib.id, TEMPLATE)
    other = image(seeded, lib, "other/same.jpg")
    assert [op.new for op in ops] == [root / f"À trier/same_{other.uid}.jpg"]
    assert ops[0].ok


def test_tidy_lists_only_misplaced_and_skips_other_statuses(seeded, refs):
    renamer.apply(seeded, renamer.preview(seeded, refs.id, TEMPLATE))
    assert renamer.preview(seeded, refs.id, TEMPLATE) == []
    two = image(seeded, refs, "two.jpg")
    seeded.update_image(two.id, status="unreadable")
    tag(seeded, image(seeded, refs, "one.jpg"), ("subject", "hand"))
    ops = renamer.preview(seeded, refs.id, TEMPLATE)
    assert [op.old.name for op in ops] == ["one.jpg"]


def test_other_filesystem_is_a_conflict(seeded, refs, monkeypatch):
    monkeypatch.setattr(renamer, "_device", lambda path: 2 if path == refs.root else 1)
    ops = renamer.preview(seeded, refs.id, TEMPLATE)
    assert ops and all(op.conflict == "destination is on another filesystem" for op in ops)


# ------------------------------------------------------------ apply / undo


def test_apply_across_libraries_then_undo(seeded, refs, tmp_path):
    inbox_root = tmp_path / "inbox"
    make_image(inbox_root / "deep" / "new.jpg", (5, 5, 5))
    inbox = seeded.add_library(inbox_root)
    library.scan(seeded, inbox)
    new = image(seeded, inbox, "new.jpg")
    tag(seeded, new, ("subject", "hand"))
    xmp.write(inbox_root / "deep" / "new.jpg", xmp.Keywords(new.uid, [], []), "sidecar")

    batch = renamer.apply(seeded, renamer.preview(seeded, inbox.id, TEMPLATE, dest_id=refs.id))
    target = refs.root / f"hand/hand_{new.uid}.jpg"
    assert target.exists() and xmp.sidecar_path(target).exists()
    moved = seeded.image(new.id)
    assert (moved.library_id, moved.rel_path) == (refs.id, f"hand/hand_{new.uid}.jpg")
    assert seeded.image_tag_ids(new.id)  # tags followed the image
    assert not (inbox_root / "deep").exists()  # emptied folder pruned
    assert inbox_root.exists()  # library root kept

    assert renamer.undo_batch(seeded, batch) == 1
    back = seeded.image(new.id)
    assert (back.library_id, back.rel_path) == (inbox.id, "deep/new.jpg")
    assert (inbox_root / "deep" / "new.jpg").exists()
    assert not (refs.root / "hand").exists()


def test_apply_keeps_the_untagged_folder(seeded, refs):
    seeded.set_layout(refs.id, TEMPLATE, "À trier")
    renamer.apply(seeded, renamer.preview(seeded, refs.id, TEMPLATE))
    three = image(seeded, refs, "three.jpg")
    tag(seeded, three, ("subject", "figure"), ("pose", "standing"))
    tag(seeded, image(seeded, refs, "two.jpg"), ("subject", "hand"))
    tag(seeded, image(seeded, refs, "one.jpg"), ("subject", "foot"))
    renamer.apply(seeded, renamer.preview(seeded, refs.id, TEMPLATE))
    assert (refs.root / "À trier").is_dir()
    assert not (refs.root / "nu").exists() and not (refs.root / "pack").exists()


def test_failure_mid_batch_puts_everything_back(seeded, refs, monkeypatch):
    ops = renamer.preview(seeded, refs.id, TEMPLATE)
    before = sorted(p.relative_to(refs.root) for p in refs.root.rglob("*"))
    real_rename, calls = os.rename, []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == len(ops) + 2:  # fail during phase 2
            raise OSError("disk on fire")
        real_rename(src, dst)

    monkeypatch.setattr(renamer.os, "rename", flaky)
    with pytest.raises(OSError):
        renamer.apply(seeded, ops)
    assert sorted(p.relative_to(refs.root) for p in refs.root.rglob("*")) == before


def test_legacy_journal_entries_still_undo(seeded, refs):
    one = image(seeded, refs, "one.jpg")
    old_rel = one.rel_path
    (refs.root / old_rel).rename(refs.root / "nu/assis/jpg/renamed.jpg")
    seeded.update_image(one.id, rel_path="nu/assis/jpg/renamed.jpg")
    seeded.add_journal(
        "rename", one.id, {"rel_path": old_rel}, {"rel_path": "nu/assis/jpg/renamed.jpg"}, batch_id="legacy"
    )
    assert renamer.undo_batch(seeded, "legacy") == 1
    assert seeded.image(one.id).rel_path == old_rel


def test_tidy_only_with_a_layout(seeded, refs):
    assert renamer.tidy(seeded, refs.id) is None
    seeded.set_layout(refs.id, TEMPLATE, "À trier")
    report = renamer.tidy(seeded, refs.id)
    assert (report.moved, report.conflicts) == (3, 0)
    assert renamer.tidy(seeded, refs.id).moved == 0


# --------------------------------------------------------------- libraries


def test_adding_a_parent_folder_absorbs_child_libraries(seeded, tmp_path):
    root = tmp_path / "Références"
    make_image(root / "boots" / "webp" / "boot.jpg", (3, 3, 3))
    make_image(root / "hands" / "hand.jpg", (4, 4, 4))
    child = seeded.add_library(root / "boots" / "webp")
    library.scan(seeded, child)
    boot = seeded.images(child.id)[0]
    tag(seeded, boot, ("subject", "foot"))
    seeded.set_setting("current_library", child.id)

    parent = seeded.add_library(root)
    assert not seeded.has_library(child.id)
    assert seeded.get_setting("current_library") == parent.id
    assert seeded.image(boot.id).rel_path == "boots/webp/boot.jpg"
    library.scan(seeded, parent)
    assert len(seeded.images()) == 2  # no duplicate of boot.jpg
    assert seeded.image_tag_ids(boot.id)


def test_adding_a_folder_inside_a_library_is_refused(seeded, refs):
    with pytest.raises(LibraryError, match="refs"):
        seeded.add_library(refs.root / "pack")
