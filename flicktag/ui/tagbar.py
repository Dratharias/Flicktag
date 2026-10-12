"""Bottom overlay: one row of tag chips per category."""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from flicktag.core.store import Category, Tag

STYLE = """
#TagBar { background: rgba(10, 10, 12, 190); border-radius: 10px; }
QLabel#category { color: #9a9aa5; font-weight: 600; letter-spacing: 1px; min-width: 96px; }
QPushButton#chip {
    color: #c9c9d1; background: transparent; border: 1px solid #3a3a44;
    border-radius: 13px; padding: 4px 12px; font-size: 13px;
}
QPushButton#chip:hover { border-color: #6a6a78; }
QPushButton#chip[state="on"] { color: #111; background: #f2c14e; border-color: #f2c14e; font-weight: 600; }
QPushButton#chip[state="suggested"] {
    color: #f2c14e; background: rgba(242, 193, 78, 40); border: 1px dashed #f2c14e;
}
"""


class TagBar(QWidget):
    toggled = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("TagBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(STYLE)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 10, 16, 10)
        self._layout.setSpacing(6)
        self._chips: dict[int, QPushButton] = {}
        self._tags: dict[int, Tag] = {}

    def rebuild(self, categories: list[Category], tags_of: Callable[[int], list[Tag]]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                _clear(item.layout())
        self._chips.clear()
        self._tags.clear()
        for category in categories:
            tags = tags_of(category.id)
            row = QHBoxLayout()
            row.setSpacing(6)
            title = QLabel(category.name.upper() + (" ·1" if category.exclusive else ""))
            title.setObjectName("category")
            title.setToolTip("exclusive: one tag at most" if category.exclusive else "several tags allowed")
            row.addWidget(title)
            for tag in tags:
                chip = QPushButton(_chip_text(tag))
                chip.setObjectName("chip")
                chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                chip.setCursor(Qt.CursorShape.PointingHandCursor)
                chip.clicked.connect(lambda _=False, tag_id=tag.id: self.toggled.emit(tag_id))
                row.addWidget(chip)
                self._chips[tag.id] = chip
                self._tags[tag.id] = tag
            row.addStretch(1)
            self._layout.addLayout(row)
        self.adjustSize()

    def update_states(self, state_of: Callable[[int], str], scores: dict[int, float]) -> None:
        for tag_id, chip in self._chips.items():
            state = state_of(tag_id)
            if chip.property("state") != state:
                chip.setProperty("state", state)
                chip.style().unpolish(chip)
                chip.style().polish(chip)
            score = scores.get(tag_id)
            chip.setToolTip(f"confidence {score:.0%}" if score is not None else "")

    def chip_state(self, tag_id: int) -> str | None:
        chip = self._chips.get(tag_id)
        return chip.property("state") if chip else None


def _chip_text(tag: Tag) -> str:
    return f"{tag.key.upper()}  {tag.name}" if tag.key else tag.name


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            _clear(item.layout())
