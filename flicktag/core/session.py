"""Sorting session state, independent of Qt.

The UI renders `Session` and forwards key presses to it; suggestions arrive
asynchronously through `set_scores`.
"""

from __future__ import annotations

from typing import Callable

from flicktag.core import queue as queue_mod
from flicktag.core.history import History
from flicktag.core.queue import QueueFilter
from flicktag.core.store import Store

ScoresToPreselect = Callable[[dict[int, float]], set[int]]


class Session:
    def __init__(
        self,
        store: Store,
        library_id: int,
        history: History,
        preselect: ScoresToPreselect,
        filter: QueueFilter = QueueFilter(),
    ):
        self.store = store
        self.library_id = library_id
        self.history = history
        self.preselect = preselect
        self.filter = filter
        self.on_validated: Callable[[int], None] | None = None
        self._scores: dict[int, dict[int, float]] = {}
        self.queue: list[int] = queue_mod.build(store, library_id, filter)
        self.index = queue_mod.start_index(store, library_id, self.queue)
        self._load()

    # ------------------------------------------------------------------ state

    @property
    def current_id(self) -> int | None:
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None

    @property
    def finished(self) -> bool:
        return self.current_id is None

    @property
    def remaining(self) -> int:
        return sum(1 for i in self.queue[self.index :] if self.store.image(i).status == "untagged")

    def upcoming(self, count: int) -> list[int]:
        """The current image and the next visible ones, for prefetching."""
        ids, i = [], self.index
        while i < len(self.queue) and len(ids) < count:
            if self._visible(self.queue[i]):
                ids.append(self.queue[i])
            i += 1
        return ids

    def _visible(self, image_id: int) -> bool:
        return self.store.image(image_id).status in queue_mod.VISIBLE

    def _load(self) -> None:
        self.checked: set[int] = set()
        self.suggested: set[int] = set()
        self.touched: set[int] = set()
        image_id = self.current_id
        if image_id is None:
            return
        self.checked = self.store.image_tag_ids(image_id)
        if image_id in self._scores:
            self._apply_scores(image_id)
        self.store.set_last_position(self.library_id, image_id)

    def scores(self, image_id: int | None = None) -> dict[int, float]:
        image_id = self.current_id if image_id is None else image_id
        return self._scores.get(image_id, {})

    def set_scores(self, image_id: int, scores: dict[int, float]) -> None:
        self._scores[image_id] = scores
        if image_id == self.current_id:
            self._apply_scores(image_id)

    def _apply_scores(self, image_id: int) -> None:
        # Only untouched, never-validated images get pre-checked suggestions.
        if self.touched or self.store.image(image_id).status != "untagged":
            return
        self.suggested = self.preselect(self._scores[image_id])
        self.checked |= self.suggested

    def tag_state(self, tag_id: int) -> str:
        """'suggested' (pre-checked by the model), 'on' or 'off'."""
        if tag_id not in self.checked:
            return "off"
        if tag_id in self.suggested and tag_id not in self.touched:
            return "suggested"
        return "on"

    # ---------------------------------------------------------------- actions

    def toggle(self, tag_id: int) -> None:
        if self.current_id is None:
            return
        self.touched.add(tag_id)
        if tag_id in self.checked:
            self.checked.discard(tag_id)
            return
        tag = self.store.tag(tag_id)
        if self.store.category(tag.category_id).exclusive:
            for other in self.store.tags(tag.category_id):
                if other.id in self.checked:
                    self.checked.discard(other.id)
                    self.touched.add(other.id)
        self.checked.add(tag_id)

    def validate_and_next(self) -> None:
        image_id = self.current_id
        if image_id is None:
            return
        self.history.validate(image_id, set(self.checked), set(self.suggested))
        if self.on_validated:
            self.on_validated(image_id)
        self.next()

    def _step(self, delta: int) -> bool:
        i = self.index + delta
        while 0 <= i < len(self.queue) and not self._visible(self.queue[i]):
            i += delta
        if i < 0:
            return False
        self.index = min(i, len(self.queue))
        self._load()
        return True

    def next(self) -> bool:
        return self._step(+1) if self.index < len(self.queue) else False

    def prev(self) -> bool:
        return self._step(-1)

    def trash(self) -> None:
        image_id = self.current_id
        if image_id is None:
            return
        self.history.trash(image_id)
        self.next()

    def undo(self) -> int | None:
        image_id = self.history.undo()
        if image_id is None:
            return None
        if image_id not in self.queue:
            self.queue.insert(self.index, image_id)
        self.index = self.queue.index(image_id)
        self._load()
        if self.on_validated:
            self.on_validated(image_id)  # the XMP must follow the restored tags
        return image_id

    def refresh_queue(self) -> None:
        """Rebuild the queue (after a rescan or vocabulary change), keeping the current image."""
        current = self.current_id
        self.queue = queue_mod.build(self.store, self.library_id, self.filter)
        if current in self.queue:
            self.index = self.queue.index(current)
            checked, suggested, touched = self.checked, self.suggested, self.touched
            self._load()
            valid = {t.id for t in self.store.tags()}
            # Keep unsaved edits on the current image; drop tags deleted meanwhile.
            self.checked, self.suggested, self.touched = checked & valid, suggested & valid, touched & valid
        else:
            self.index = 0
            self._load()

    def jump_to(self, image_id: int) -> None:
        if image_id in self.queue:
            self.index = self.queue.index(image_id)
            self._load()
