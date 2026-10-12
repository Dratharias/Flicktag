"""Which images a sorting session walks through, and in which order."""

from __future__ import annotations

from dataclasses import dataclass

from flicktag.core.store import Store

VISIBLE = ("untagged", "tagged")


@dataclass(frozen=True)
class QueueFilter:
    """kind: 'all' (untagged first), 'untagged', 'category' or 'tag'."""

    kind: str = "all"
    target_id: int | None = None

    def label(self, store: Store) -> str:
        if self.kind == "category" and self.target_id is not None:
            return f"category: {store.category(self.target_id).name}"
        if self.kind == "tag" and self.target_id is not None:
            return f"tag: {store.tag(self.target_id).name}"
        return {"all": "all", "untagged": "untagged only"}.get(self.kind, self.kind)


def build(store: Store, library_id: int, filter: QueueFilter = QueueFilter()) -> list[int]:
    if filter.kind == "category":
        rows = store.query(
            "SELECT DISTINCT i.id, i.rel_path FROM images i JOIN image_tags it ON it.image_id = i.id "
            "JOIN tags t ON t.id = it.tag_id WHERE i.library_id = ? AND t.category_id = ? "
            "AND i.status IN ('untagged', 'tagged') ORDER BY i.rel_path",
            (library_id, filter.target_id),
        )
        return [r["id"] for r in rows]
    if filter.kind == "tag":
        rows = store.query(
            "SELECT i.id FROM images i JOIN image_tags it ON it.image_id = i.id "
            "WHERE i.library_id = ? AND it.tag_id = ? AND i.status IN ('untagged', 'tagged') "
            "ORDER BY i.rel_path",
            (library_id, filter.target_id),
        )
        return [r["id"] for r in rows]
    untagged = [i.id for i in store.images(library_id, statuses=("untagged",))]
    if filter.kind == "untagged":
        return untagged
    tagged = [i.id for i in store.images(library_id, statuses=("tagged",))]
    return untagged + tagged


def start_index(store: Store, library_id: int, queue: list[int]) -> int:
    """Resume at the last image viewed if it is still in the queue, else at the start."""
    last = store.library(library_id).last_position
    if last in queue and store.image(last).status == "untagged":
        return queue.index(last)
    return 0
