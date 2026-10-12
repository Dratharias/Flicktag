"""Bring an image's XMP in line with its tags in the database."""

from __future__ import annotations

from flicktag.core import xmp
from flicktag.core.library import hash_file
from flicktag.core.store import Store

DEFAULT_MODE = "embedded"


def keywords_for(store: Store, image_id: int) -> xmp.Keywords:
    image = store.image(image_id)
    categories = {c.id: c for c in store.categories()}
    tags = store.image_tags(image_id)
    return xmp.Keywords(
        uid=image.uid,
        subjects=[t.name for t in tags],
        hierarchical=[f"{categories[t.category_id].name}|{t.name}" for t in tags],
    )


def sync(store: Store, image_id: int) -> bool:
    """Write the XMP for one image. True on success; on failure xmp_pending stays set."""
    mode = store.get_setting("xmp_mode", DEFAULT_MODE)
    image = store.image(image_id)
    if image.status in ("trashed", "missing", "unreadable"):
        return False
    path = store.image_path(image)
    try:
        written = xmp.write(path, keywords_for(store, image_id), mode)
    except Exception as error:
        store.add_journal("xmp_error", image_id, None, {"error": str(error)})
        store.update_image(image_id, xmp_pending=1)
        return False
    fields: dict = {"xmp_pending": 0}
    if written == path:
        fields["content_hash"] = hash_file(path)
    store.update_image(image_id, **fields)
    return True


def pending(store: Store) -> list[int]:
    rows = store.query(
        "SELECT id FROM images WHERE xmp_pending = 1 AND status IN ('tagged', 'untagged')"
    )
    return [r["id"] for r in rows]


def retry_pending(store: Store) -> tuple[int, int]:
    """Retry every pending write; return (succeeded, failed)."""
    ok = failed = 0
    for image_id in pending(store):
        if sync(store, image_id):
            ok += 1
        else:
            failed += 1
    return ok, failed
