"""Undo stack for the sorting session: tag validations and trash moves.

Rename batches have their own undo (see renamer.undo_batch).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from flicktag.core import tagging, trashcan, xmp
from flicktag.core.store import Store

TrashFn = Callable[[str], bool]
RestoreFn = Callable[[str], bool]


class HistoryError(RuntimeError):
    pass


class History:
    def __init__(
        self,
        store: Store,
        trash_fn: TrashFn,
        restore_fn: RestoreFn | None = None,
    ):
        self.store = store
        self.trash_fn = trash_fn
        self.restore_fn = restore_fn or (lambda path: trashcan.restore(Path(path)))
        self._stack: list[int] = []

    def __len__(self) -> int:
        return len(self._stack)

    def push(self, journal_id: int) -> None:
        self._stack.append(journal_id)

    def validate(self, image_id: int, chosen: set[int], suggested: set[int]) -> int:
        entry = tagging.validate(self.store, image_id, chosen, suggested)
        self.push(entry)
        return entry

    def trash(self, image_id: int) -> None:
        image = self.store.image(image_id)
        path = self.store.image_path(image)
        sidecar = xmp.sidecar_path(path)
        if not self.trash_fn(str(path)):
            raise HistoryError(f"could not move {path.name} to the trash")
        had_sidecar = sidecar.exists() and self.trash_fn(str(sidecar))
        before = {"status": image.status, "path": str(path), "sidecar": str(sidecar) if had_sidecar else None}
        with self.store.transaction():
            self.store.update_image(image_id, status="trashed")
            self.push(self.store.add_journal("trash", image_id, before, {"status": "trashed"}))

    def undo(self) -> int | None:
        """Revert the last action; return the image it concerned, or None if empty."""
        if not self._stack:
            return None
        entry_id = self._stack[-1]
        entry = self.store.journal_entry(entry_id)
        before = json.loads(entry["before"])
        image_id = entry["image_id"]
        if entry["kind"] == "tags":
            tagging.restore(self.store, image_id, before)
        elif entry["kind"] == "trash":
            if not self.restore_fn(before["path"]):
                raise HistoryError(f"could not restore {before['path']} from the trash")
            if before.get("sidecar"):
                self.restore_fn(before["sidecar"])
            self.store.update_image(image_id, status=before["status"])
        else:
            raise HistoryError(f"cannot undo {entry['kind']}")
        self.store.mark_reverted([entry_id])
        self._stack.pop()
        return image_id
