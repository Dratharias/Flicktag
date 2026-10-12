"""Validating an image: tags, suggestion feedback, status — one transaction."""

from __future__ import annotations

import time

from flicktag.core.store import Store


def _feedback_rows(store: Store, image_id: int, tag_ids: set[int]) -> dict[int, int | None]:
    rows = store.query("SELECT tag_id, label FROM feedback WHERE image_id = ?", (image_id,))
    labels = {r["tag_id"]: r["label"] for r in rows}
    return {tag_id: labels.get(tag_id) for tag_id in tag_ids}


def snapshot(store: Store, image_id: int, feedback_tags: set[int]) -> dict:
    image = store.image(image_id)
    rows = store.query("SELECT tag_id, source FROM image_tags WHERE image_id = ?", (image_id,))
    return {
        "status": image.status,
        "tagged_at": image.tagged_at,
        "tags": {str(r["tag_id"]): r["source"] for r in rows},
        "feedback": {str(k): v for k, v in _feedback_rows(store, image_id, feedback_tags).items()},
    }


def restore(store: Store, image_id: int, state: dict) -> None:
    """Put an image back into a snapshot() state."""
    now = time.time()
    with store.transaction():
        store.execute("DELETE FROM image_tags WHERE image_id = ?", (image_id,))
        for tag_id, source in state["tags"].items():
            store.execute(
                "INSERT OR IGNORE INTO image_tags(image_id, tag_id, source, created_at) VALUES (?, ?, ?, ?)",
                (image_id, int(tag_id), source, now),
            )
        for tag_id, label in state["feedback"].items():
            if label is None:
                store.execute("DELETE FROM feedback WHERE image_id = ? AND tag_id = ?", (image_id, int(tag_id)))
            else:
                store.execute(
                    "INSERT INTO feedback(image_id, tag_id, label, created_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(image_id, tag_id) DO UPDATE SET label = excluded.label",
                    (image_id, int(tag_id), label, now),
                )
        store.update_image(image_id, status=state["status"], tagged_at=state["tagged_at"], xmp_pending=1)


def validate(store: Store, image_id: int, chosen: set[int], suggested: set[int]) -> int:
    """Store the chosen tags for an image; return the journal entry id.

    Feedback: a suggested tag that was kept is a positive example, one that was
    unchecked a negative; a tag added by hand is a positive example.
    """
    feedback = {tag_id: 1 for tag_id in chosen}
    feedback.update({tag_id: 0 for tag_id in suggested - chosen})
    with store.transaction():
        before = snapshot(store, image_id, set(feedback))
        now = time.time()
        after = {
            "status": "tagged",
            "tagged_at": now,
            "tags": {str(t): ("suggested" if t in suggested else "manual") for t in chosen},
            "feedback": {str(k): v for k, v in feedback.items()},
        }
        restore(store, image_id, after)
        return store.add_journal("tags", image_id, before, after)
