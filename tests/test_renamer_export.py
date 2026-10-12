import io
import json

import pytest
from conftest import make_image, tag_id

from flicktag.core import export, library, renamer, xmp
from flicktag.core.tagging import validate


@pytest.fixture
def lib(seeded, photos):
    lib = seeded.add_library(photos)
    library.scan(seeded, lib)
    return lib


def by_name(store, lib):
    return {i.rel_path: i for i in store.images(lib.id)}


def test_parse_template_rejects_unknown_and_requires_uid(seeded):
    slugs = {c.slug for c in seeded.categories()}
    with pytest.raises(renamer.TemplateError):
        renamer.parse_template("{subject}_{nope}_{uid}", slugs)
    with pytest.raises(renamer.TemplateError):
        renamer.parse_template("{subject}_{pose}", slugs)


def test_render_drops_missing_categories_with_separator(seeded):
    parts = renamer.parse_template("{subject}_{pose}_{uid}", {c.slug for c in seeded.categories()})
    assert renamer.render(parts, {"subject": "hand", "pose": "standing"}, "abcd1234") == "hand_standing_abcd1234"
    assert renamer.render(parts, {"pose": "standing"}, "abcd1234") == "standing_abcd1234"
    assert renamer.render(parts, {}, "abcd1234") == "abcd1234"


def test_render_truncates_tags_never_uid(seeded):
    parts = renamer.parse_template("{subject}_{pose}_{uid}", {c.slug for c in seeded.categories()})
    stem = renamer.render(parts, {"subject": "x" * 70, "pose": "y" * 40}, "abcd1234")
    assert len(stem) <= renamer.MAX_STEM
    assert stem.endswith("_abcd1234")


def test_preview_apply_and_undo(seeded, lib, photos):
    images = by_name(seeded, lib)
    a = images["a.jpg"]
    hand = tag_id(seeded, "subject", "hand")
    figure = tag_id(seeded, "subject", "figure")
    standing = tag_id(seeded, "pose", "standing")
    validate(seeded, a.id, {hand, figure, standing}, set())
    xmp.write(photos / "a.jpg", xmp.Keywords(a.uid, [], []), "sidecar")

    ops = renamer.preview(seeded, lib.id, "{subject}_{pose}_{uid}")
    new_a = next(op for op in ops if op.image_id == a.id)
    # Multiple category: first tag by position (figure comes before hand).
    assert new_a.new.name == f"figure_standing_{a.uid}.jpg"
    assert all(op.ok for op in ops)

    batch = renamer.apply(seeded, ops)
    assert (photos / new_a.new.name).exists()
    assert xmp.sidecar_path(photos / new_a.new.name).exists()
    assert seeded.image(a.id).rel_path == new_a.new.name
    assert renamer.preview(seeded, lib.id, "{subject}_{pose}_{uid}") == []

    assert renamer.undo_batch(seeded, batch) == len(ops)
    assert (photos / "a.jpg").exists() and xmp.sidecar_path(photos / "a.jpg").exists()
    assert seeded.image(a.id).rel_path == "a.jpg"
    assert renamer.batches(seeded)[0]["reverted_at"] is not None


def test_preview_flags_foreign_file_conflict(seeded, lib, photos):
    a = by_name(seeded, lib)["a.jpg"]
    validate(seeded, a.id, {tag_id(seeded, "pose", "standing")}, set())  # no subject -> name is just the uid
    make_image(photos / f"{a.uid}.jpg", (1, 2, 3))  # unrelated file squatting the target
    ops = renamer.preview(seeded, lib.id, "{subject}_{uid}")
    op = next(op for op in ops if op.image_id == a.id)
    assert op.conflict
    renamer.apply(seeded, ops)
    assert (photos / "a.jpg").exists()


def test_swapping_names_works(seeded, tmp_path):
    root = tmp_path / "swap"
    make_image(root / "x.jpg", (10, 0, 0))
    make_image(root / "y.jpg", (0, 10, 0))
    lib = seeded.add_library(root)
    library.scan(seeded, lib)
    x, y = by_name(seeded, lib)["x.jpg"], by_name(seeded, lib)["y.jpg"]
    x_bytes = (root / "x.jpg").read_bytes()
    ops = [renamer.RenameOp(x.id, root / "x.jpg", root / "y.jpg"), renamer.RenameOp(y.id, root / "y.jpg", root / "x.jpg")]
    renamer.apply(seeded, ops)
    assert (root / "y.jpg").read_bytes() == x_bytes
    assert seeded.image(x.id).rel_path == "y.jpg" and seeded.image(y.id).rel_path == "x.jpg"


def test_undo_skips_files_renamed_again(seeded, lib, photos):
    ops = renamer.preview(seeded, lib.id, "{uid}")
    batch = renamer.apply(seeded, ops)
    a = by_name(seeded, lib)
    first = next(iter(a.values()))
    (photos / first.rel_path).rename(photos / "manual.jpg")
    seeded.update_image(first.id, rel_path="manual.jpg")
    assert renamer.undo_batch(seeded, batch) == len(ops) - 1


def test_export_jsonl(seeded, lib, photos, fake_trash):
    images = by_name(seeded, lib)
    validate(seeded, images["a.jpg"].id, {tag_id(seeded, "pose", "sitting"), tag_id(seeded, "subject", "hand")}, set())
    seeded.update_image(images["b.png"].id, status="trashed")
    out = io.StringIO()
    count = export.write_jsonl(seeded, out)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[0] == {"format_version": 1}
    assert count == len(lines) - 1 == 2
    a = next(r for r in lines[1:] if r["path"].endswith("a.jpg"))
    assert a["tags"] == {"subject": ["hand"], "pose": ["sitting"]}
    assert a["status"] == "tagged" and a["library"] == "photos"
    assert a["width"] == 64 and a["height"] == 48
