"""Stable JSON Lines export — the data contract for the quick-poses app.

Line 1 is a header {"format_version": 1}; every other line is one image.
See docs/FORMAT.md.
"""

from __future__ import annotations

import json
from typing import IO, Iterator

from flicktag.core.store import Store

FORMAT_VERSION = 1


def records(store: Store, library_id: int | None = None) -> Iterator[dict]:
    categories = {c.id: c for c in store.categories()}
    libraries = {lib.id: lib for lib in store.libraries()}
    for image in store.images(library_id, statuses=("tagged", "untagged")):
        tags: dict[str, list[str]] = {}
        for tag in store.image_tags(image.id):
            tags.setdefault(categories[tag.category_id].slug, []).append(tag.slug)
        library = libraries[image.library_id]
        yield {
            "uid": image.uid,
            "content_hash": image.content_hash,
            "library": library.name,
            "path": str(library.root / image.rel_path),
            "width": image.width,
            "height": image.height,
            "status": image.status,
            "tags": tags,
        }


def write_jsonl(store: Store, out: IO[str], library_id: int | None = None) -> int:
    """Write the export; return the number of image records."""
    out.write(json.dumps({"format_version": FORMAT_VERSION}) + "\n")
    count = 0
    for record in records(store, library_id):
        out.write(json.dumps(record, ensure_ascii=False) + "\n")
        count += 1
    return count
