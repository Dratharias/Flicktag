import shutil

from conftest import make_image

from flicktag.core import library, xmp, xmpsync
from flicktag.core.tagging import validate
from conftest import tag_id


def test_scan_adds_supported_images_only(store, photos):
    (photos / "notes.txt").write_text("x")
    (photos / ".hidden").mkdir()
    make_image(photos / ".hidden" / "x.jpg")
    lib = store.add_library(photos)
    report = library.scan(store, lib)
    assert report.added == 3
    paths = sorted(i.rel_path for i in store.images(lib.id))
    assert paths == ["a.jpg", "b.png", "sub/c.webp"]
    assert all(len(i.uid) == 8 for i in store.images(lib.id))


def test_rescan_is_stable(store, photos):
    lib = store.add_library(photos)
    library.scan(store, lib)
    report = library.scan(store, lib)
    assert (report.added, report.moved, report.missing) == (0, 0, 0)


def test_unreadable_file_is_flagged(store, photos):
    (photos / "broken.jpg").write_bytes(b"not an image")
    lib = store.add_library(photos)
    report = library.scan(store, lib)
    assert report.unreadable == ["broken.jpg"]
    assert store.images(lib.id, statuses=("unreadable",))[0].rel_path == "broken.jpg"


def test_moved_file_keeps_identity_by_hash(store, photos):
    lib = store.add_library(photos)
    library.scan(store, lib)
    before = {i.rel_path: i for i in store.images(lib.id)}["a.jpg"]
    (photos / "moved").mkdir()
    shutil.move(photos / "a.jpg", photos / "moved" / "renamed.jpg")
    report = library.scan(store, lib)
    assert report.moved == 1 and report.added == 0
    assert store.image(before.id).rel_path == "moved/renamed.jpg"


def test_moved_file_keeps_identity_by_uid_after_xmp_write(seeded, photos):
    lib = seeded.add_library(photos)
    library.scan(seeded, lib)
    image = {i.rel_path: i for i in seeded.images(lib.id)}["a.jpg"]
    validate(seeded, image.id, {tag_id(seeded, "pose", "standing")}, set())
    assert xmpsync.sync(seeded, image.id)
    # Content changed (XMP embedded) *and* path changed: only the uid can match.
    shutil.move(photos / "a.jpg", photos / "elsewhere.jpg")
    seeded.update_image(image.id, content_hash="stale")
    library.scan(seeded, lib)
    assert seeded.image(image.id).rel_path == "elsewhere.jpg"
    assert seeded.image(image.id).status == "tagged"


def test_copy_gets_its_own_identity(store, photos):
    lib = store.add_library(photos)
    library.scan(store, lib)
    shutil.copy(photos / "a.jpg", photos / "a-copy.jpg")
    report = library.scan(store, lib)
    assert report.added == 1
    uids = [i.uid for i in store.images(lib.id)]
    assert len(uids) == len(set(uids))


def test_missing_then_found_again(seeded, photos):
    lib = seeded.add_library(photos)
    library.scan(seeded, lib)
    image = {i.rel_path: i for i in seeded.images(lib.id)}["b.png"]
    validate(seeded, image.id, {tag_id(seeded, "subject", "hand")}, set())
    shutil.move(photos / "b.png", photos.parent / "b.png")
    assert library.scan(seeded, lib).missing == 1
    assert seeded.image(image.id).status == "missing"
    assert seeded.image_tag_ids(image.id)  # tags kept
    shutil.move(photos.parent / "b.png", photos / "b.png")
    library.scan(seeded, lib)
    assert seeded.image(image.id).status == "tagged"


def test_xmp_roundtrip_preserves_other_fields(tmp_path):
    path = make_image(tmp_path / "x.jpg")
    import pyexiv2

    with pyexiv2.Image(str(path)) as im:
        im.modify_xmp({"Xmp.dc.creator": ["Someone"]})
    kw = xmp.Keywords(uid="abcd1234", subjects=["standing"], hierarchical=["Pose|standing"])
    assert xmp.write(path, kw, "embedded") == path
    data = xmp.read(path)
    assert data["Xmp.dc.subject"] == ["standing"]
    assert data["Xmp.lr.hierarchicalSubject"] == ["Pose|standing"]
    assert data["Xmp.dc.creator"] == ["Someone"]
    assert xmp.read_uid(path) == "abcd1234"
    # Clearing tags removes the keyword fields but keeps the uid.
    xmp.write(path, xmp.Keywords("abcd1234", [], []), "embedded")
    data = xmp.read(path)
    assert "Xmp.dc.subject" not in data and data["Xmp.flicktag.uid"] == "abcd1234"


def test_gif_and_sidecar_mode_use_sidecar(tmp_path):
    gif = make_image(tmp_path / "anim.gif", fmt="GIF")
    jpg = make_image(tmp_path / "side.jpg")
    kw = xmp.Keywords(uid="ffff0000", subjects=["hand"], hierarchical=["Subject|hand"])
    original = jpg.read_bytes()
    assert xmp.write(gif, kw, "embedded") == xmp.sidecar_path(gif)
    assert xmp.write(jpg, kw, "sidecar") == xmp.sidecar_path(jpg)
    assert jpg.read_bytes() == original
    assert xmp.read_uid(gif) == "ffff0000"
    assert xmp.read(jpg)["Xmp.dc.subject"] == ["hand"]


def test_off_mode_writes_nothing(tmp_path):
    jpg = make_image(tmp_path / "x.jpg")
    original = jpg.read_bytes()
    assert xmp.write(jpg, xmp.Keywords("a", ["b"], ["c|b"]), "off") is None
    assert jpg.read_bytes() == original and not xmp.sidecar_path(jpg).exists()


def test_sync_updates_hash_and_clears_pending(seeded, photos):
    lib = seeded.add_library(photos)
    library.scan(seeded, lib)
    image = seeded.images(lib.id)[0]
    validate(seeded, image.id, {tag_id(seeded, "subject", "figure")}, set())
    assert seeded.image(image.id).xmp_pending
    assert xmpsync.sync(seeded, image.id)
    after = seeded.image(image.id)
    assert not after.xmp_pending
    assert after.content_hash == library.hash_file(seeded.image_path(after))
    assert after.content_hash != image.content_hash


def test_sync_failure_keeps_pending(seeded, photos):
    lib = seeded.add_library(photos)
    library.scan(seeded, lib)
    image = seeded.images(lib.id)[0]
    validate(seeded, image.id, set(), set())
    path = seeded.image_path(image)
    path.chmod(0o444)
    path.parent.chmod(0o555)
    try:
        assert not xmpsync.sync(seeded, image.id)
    finally:
        path.parent.chmod(0o755)
        path.chmod(0o644)
    assert seeded.image(image.id).xmp_pending
    assert xmpsync.pending(seeded) == [image.id]
