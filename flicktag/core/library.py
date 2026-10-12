"""Library scanning and image identity.

A file is matched to a known image by, in order: (library, rel_path), the
`flicktag:uid` stored in its XMP, then its content hash. Unmatched files become
new images; known images whose file is gone become 'missing'.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image as PILImage

from flicktag.core import xmp
from flicktag.core.store import Library, Store

SUPPORTED = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
UID_LENGTH = 8


@dataclass
class ScanReport:
    added: int = 0
    moved: int = 0
    missing: int = 0
    unreadable: list[str] = field(default_factory=list)


def hash_file(path: Path) -> str:
    digest = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_size(path: Path) -> tuple[int, int] | None:
    """Pixel size, or None when the file is not a decodable image."""
    try:
        with PILImage.open(path) as image:
            image.verify()
        with PILImage.open(path) as image:
            return image.size
    except Exception:
        return None


def iter_images(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or Path(name).suffix.lower() not in SUPPORTED:
                continue
            yield Path(dirpath) / name


def new_uid(store: Store, content_hash: str) -> str:
    length = UID_LENGTH
    while length <= len(content_hash):
        candidate = content_hash[:length]
        if not store.query("SELECT 1 FROM images WHERE uid = ?", (candidate,)):
            return candidate
        length += 2
    raise RuntimeError(f"cannot derive a unique uid from {content_hash}")


def _status_when_found(store: Store, image_id: int) -> str:
    return "tagged" if store.image_tag_ids(image_id) else "untagged"


def scan(store: Store, library: Library) -> ScanReport:
    report = ScanReport()
    seen: set[int] = set()
    root = library.root

    for path in iter_images(root):
        rel_path = path.relative_to(root).as_posix()
        rows = store.query(
            "SELECT id, status FROM images WHERE library_id = ? AND rel_path = ?",
            (library.id, rel_path),
        )
        if rows:
            image_id, status = rows[0]["id"], rows[0]["status"]
            seen.add(image_id)
            if status == "missing":
                store.update_image(image_id, status=_status_when_found(store, image_id))
            continue

        content_hash = hash_file(path)
        uid = xmp.read_uid(path)
        match = None
        if uid:
            match = store.query("SELECT id, status FROM images WHERE uid = ?", (uid,))
        if not match:
            match = store.query(
                "SELECT id, status FROM images WHERE content_hash = ? AND id NOT IN "
                f"({','.join(str(i) for i in seen) or 'NULL'})",
                (content_hash,),
            )
        # Only adopt a known image if its old file is really gone; otherwise this
        # is a copy and deserves its own identity.
        if match:
            known = store.image(match[0]["id"])
            if known.id in seen or store.image_path(known).exists():
                match = None

        size = image_size(path)
        if match:
            image_id = match[0]["id"]
            status = match[0]["status"]
            if status in ("missing", "unreadable"):
                status = _status_when_found(store, image_id) if size else "unreadable"
            store.update_image(
                image_id,
                library_id=library.id,
                rel_path=rel_path,
                content_hash=content_hash,
                status=status,
            )
            report.moved += 1
        else:
            cur = store.execute(
                "INSERT INTO images(uid, content_hash, library_id, rel_path, width, height, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    new_uid(store, content_hash),
                    content_hash,
                    library.id,
                    rel_path,
                    size[0] if size else None,
                    size[1] if size else None,
                    "untagged" if size else "unreadable",
                ),
            )
            image_id = cur.lastrowid
            report.added += 1
        if not size:
            report.unreadable.append(rel_path)
        seen.add(image_id)

    for image in store.images(library.id):
        if image.id not in seen and image.status not in ("trashed", "missing"):
            store.update_image(image.id, status="missing")
            report.missing += 1
    return report
