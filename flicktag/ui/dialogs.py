"""Secondary windows: new tag, vocabulary editor, rename, settings, lists."""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from flicktag.core import renamer, vocab, xmp
from flicktag.core.store import Store, Tag
from flicktag.suggest.client import DEFAULT_URL
from flicktag.suggest.engine import DEFAULT_THRESHOLD


def _error(parent: QWidget, message: str) -> None:
    QMessageBox.warning(parent, "Flicktag", message)


# --------------------------------------------------------------------- new tag


class NewTagDialog(QDialog):
    """Quick field: pick a category, type a name (autocompleted). Existing tags are reused."""

    def __init__(self, store: Store, parent: QWidget | None = None, category_id: int | None = None):
        super().__init__(parent)
        self.store = store
        self.result_tag: Tag | None = None
        self.setWindowTitle("New tag")
        layout = QFormLayout(self)
        self.category = QComboBox()
        for category in store.categories():
            self.category.addItem(category.name, category.id)
        self.category.addItem("+ new category…", None)
        if category_id is not None:
            self.category.setCurrentIndex(max(self.category.findData(category_id), 0))
        self.new_category = QLineEdit()
        self.new_category.setPlaceholderText("category name")
        self.new_category.setVisible(False)
        self.exclusive = QCheckBox("exclusive (one tag at most)")
        self.exclusive.setVisible(False)
        self.name = QLineEdit()
        self.name.setPlaceholderText("tag name")
        self.prompt = QLineEdit()
        self.prompt.setPlaceholderText("optional English description for suggestions")
        layout.addRow("Category", self.category)
        layout.addRow("", self.new_category)
        layout.addRow("", self.exclusive)
        layout.addRow("Tag", self.name)
        layout.addRow("Prompt", self.prompt)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.category.currentIndexChanged.connect(self._category_changed)
        self._category_changed()
        self.name.setFocus()

    def _category_changed(self) -> None:
        creating = self.category.currentData() is None
        self.new_category.setVisible(creating)
        self.exclusive.setVisible(creating)
        names = [] if creating else [t.name for t in self.store.tags(self.category.currentData())]
        completer = QCompleter(names, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.name.setCompleter(completer)

    def _accept(self) -> None:
        try:
            category_id = self.category.currentData()
            if category_id is None:
                category_id = vocab.add_category(
                    self.store, self.new_category.text(), exclusive=self.exclusive.isChecked()
                ).id
            existing = vocab.find_tag(self.store, category_id, self.name.text())
            self.result_tag = existing or vocab.add_tag(
                self.store, category_id, self.name.text(), prompt=self.prompt.text().strip() or None
            )
        except vocab.VocabError as error:
            _error(self, str(error))
            return
        self.accept()


# ------------------------------------------------------------------ vocabulary


class VocabularyDialog(QDialog):
    """Tree of categories and tags with an edit form for the selection."""

    def __init__(self, store: Store, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self.changed = False
        self.setWindowTitle("Vocabulary")
        self.resize(760, 520)
        root = QHBoxLayout(self)

        left = QVBoxLayout()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Key"])
        self.tree.setColumnWidth(0, 260)
        self.tree.itemSelectionChanged.connect(self._load_selection)
        left.addWidget(self.tree)
        row = QHBoxLayout()
        for label, slot in (
            ("+ Category", self._add_category),
            ("+ Tag", self._add_tag),
            ("↑", lambda: self._move(-1)),
            ("↓", lambda: self._move(+1)),
            ("Delete", self._delete),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            row.addWidget(button)
        left.addLayout(row)
        root.addLayout(left, 3)

        self.form = QFormLayout()
        self.name = QLineEdit()
        self.key = QLineEdit()
        self.key.setMaxLength(2)
        self.key.setPlaceholderText("A, 1 or +A for Shift+A")
        self.prompt = QLineEdit()
        self.exclusive = QCheckBox("exclusive (one tag at most)")
        self.rank = QSpinBox()
        self.rank.setRange(0, 99)
        self.rank.setSpecialValueText("not used")
        self.form.addRow("Name", self.name)
        self.form.addRow("Shortcut", self.key)
        self.form.addRow("Prompt", self.prompt)
        self.form.addRow("", self.exclusive)
        self.form.addRow("Rename rank", self.rank)
        save = QPushButton("Save")
        save.clicked.connect(self._save)
        self.form.addRow(save)
        hint = QLabel(
            "Prompt: short English description used for zero-shot suggestions.\n"
            "Rename rank: position in the default rename template (\"not used\" = left out)."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        self.form.addRow(hint)
        side = QWidget()
        side.setLayout(self.form)
        root.addWidget(side, 2)
        self._reload()

    def _reload(self, select: tuple[str, int] | None = None) -> None:
        self.tree.clear()
        for category in self.store.categories():
            parent = QTreeWidgetItem([category.name + ("  (exclusive)" if category.exclusive else ""), ""])
            parent.setData(0, Qt.ItemDataRole.UserRole, ("category", category.id))
            self.tree.addTopLevelItem(parent)
            for tag in self.store.tags(category.id):
                child = QTreeWidgetItem([tag.name, vocab.key_label(tag.key) if tag.key else ""])
                child.setData(0, Qt.ItemDataRole.UserRole, ("tag", tag.id))
                parent.addChild(child)
                if select == ("tag", tag.id):
                    self.tree.setCurrentItem(child)
            if select == ("category", category.id):
                self.tree.setCurrentItem(parent)
        self.tree.expandAll()
        self._load_selection()

    def _selected(self) -> tuple[str, int] | None:
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _load_selection(self) -> None:
        selected = self._selected()
        is_tag = bool(selected) and selected[0] == "tag"
        is_category = bool(selected) and selected[0] == "category"
        for widget in (self.key, self.prompt):
            widget.setEnabled(is_tag)
        for widget in (self.exclusive, self.rank):
            widget.setEnabled(is_category)
        self.name.setEnabled(bool(selected))
        if is_tag:
            tag = self.store.tag(selected[1])
            self.name.setText(tag.name)
            self.key.setText(vocab.format_key(tag.key))
            self.prompt.setText(tag.prompt or "")
        elif is_category:
            category = self.store.category(selected[1])
            self.name.setText(category.name)
            self.exclusive.setChecked(category.exclusive)
            self.rank.setValue(category.rename_rank or 0)
            self.key.clear()
            self.prompt.clear()

    def _save(self) -> None:
        selected = self._selected()
        if not selected:
            return
        try:
            if selected[0] == "tag":
                vocab.update_tag(
                    self.store, selected[1], self.name.text(), vocab.parse_key(self.key.text()), self.prompt.text()
                )
            else:
                vocab.update_category(
                    self.store, selected[1], self.name.text(), self.exclusive.isChecked(), self.rank.value() or None
                )
        except vocab.VocabError as error:
            _error(self, str(error))
            return
        self.changed = True
        self._reload(selected)

    def _add_category(self) -> None:
        dialog = _NameDialog("New category", self, with_exclusive=True)
        if dialog.exec():
            try:
                category = vocab.add_category(self.store, dialog.value(), exclusive=dialog.exclusive.isChecked())
            except vocab.VocabError as error:
                _error(self, str(error))
                return
            self.changed = True
            self._reload(("category", category.id))

    def _add_tag(self) -> None:
        selected = self._selected()
        category_id = None
        if selected:
            category_id = selected[1] if selected[0] == "category" else self.store.tag(selected[1]).category_id
        dialog = NewTagDialog(self.store, self, category_id)
        if dialog.exec() and dialog.result_tag:
            self.changed = True
            self._reload(("tag", dialog.result_tag.id))

    def _move(self, delta: int) -> None:
        selected = self._selected()
        if not selected:
            return
        vocab.move(self.store, "categories" if selected[0] == "category" else "tags", selected[1], delta)
        self.changed = True
        self._reload(selected)

    def _delete(self) -> None:
        selected = self._selected()
        if not selected:
            return
        kind, item_id = selected
        if kind == "tag":
            used = self.store.query("SELECT COUNT(*) FROM image_tags WHERE tag_id = ?", (item_id,))[0][0]
            question = f"Delete tag '{self.store.tag(item_id).name}'? It is set on {used} image(s)."
        else:
            used = self.store.query(
                "SELECT COUNT(*) FROM image_tags it JOIN tags t ON t.id = it.tag_id WHERE t.category_id = ?",
                (item_id,),
            )[0][0]
            question = f"Delete category '{self.store.category(item_id).name}' and all its tags ({used} uses)?"
        if QMessageBox.question(self, "Flicktag", question) != QMessageBox.StandardButton.Yes:
            return
        (vocab.delete_tag if kind == "tag" else vocab.delete_category)(self.store, item_id)
        self.changed = True
        self._reload()


class _NameDialog(QDialog):
    def __init__(self, title: str, parent: QWidget | None = None, with_exclusive: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QFormLayout(self)
        self.name = QLineEdit()
        layout.addRow("Name", self.name)
        self.exclusive = QCheckBox("exclusive (one tag at most)")
        if with_exclusive:
            layout.addRow("", self.exclusive)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def value(self) -> str:
        return self.name.text()


# -------------------------------------------------------------------- organize


class OrganizeDialog(QDialog):
    """Move the images of a library to where a destination library's layout puts them.

    With `tidy`, the destination is the library itself and the preview runs at once.
    Apply saves the template and untagged folder as the destination's layout.
    """

    def __init__(self, store: Store, library_id: int, tidy: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self.library_id = library_id
        self.ops: list[renamer.RenameOp] = []
        self.applied = False
        self.setWindowTitle("Tidy library" if tidy else "Organize from tags")
        self.resize(1000, 650)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.destination = QComboBox()
        for library in store.libraries():
            self.destination.addItem(library.name, library.id)
        self.destination.setCurrentIndex(self.destination.findData(library_id))
        self.destination.setEnabled(not tidy)
        self.destination.currentIndexChanged.connect(self._load_layout)
        self.template = QLineEdit()
        placeholders = " ".join("{%s}" % c.slug for c in store.categories()) + " {uid}"
        self.template.setToolTip("Placeholders: " + placeholders)
        self.untagged_dir = QLineEdit()
        form.addRow("Destination library", self.destination)
        form.addRow("Template", self.template)
        form.addRow("Untagged folder", self.untagged_dir)
        layout.addLayout(form)
        hint = QLabel(
            "'/' separates folders; the last part is the file name and needs {uid}. "
            "Untagged images go to the untagged folder with their original name.\n"
            "Placeholders: " + placeholders
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        layout.addWidget(hint)
        preview = QPushButton("Preview")
        preview.clicked.connect(self._preview)
        layout.addWidget(preview)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Current path", "New path", "Problem"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        self.summary = QLabel("")
        layout.addWidget(self.summary)
        self.apply_button = QPushButton("Apply")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        layout.addWidget(self.apply_button)

        layout.addWidget(QLabel("Previous batches"))
        self.batches = QListWidget()
        self.batches.setMaximumHeight(120)
        layout.addWidget(self.batches)
        undo = QPushButton("Undo selected batch")
        undo.clicked.connect(self._undo)
        layout.addWidget(undo)
        self._load_layout()
        self._load_batches()
        # Apply must use the ops of the text it saves: any edit asks for a new preview.
        for field in (self.template, self.untagged_dir):
            field.textEdited.connect(lambda _text: self.apply_button.setEnabled(False))
        if tidy:
            self._preview()

    def _dest(self):
        return self.store.library(self.destination.currentData())

    def _load_layout(self) -> None:
        dest = self._dest()
        self.template.setText(dest.layout or renamer.default_template(self.store))
        self.untagged_dir.setText(dest.untagged_dir or renamer.DEFAULT_UNTAGGED_DIR)
        self.ops = []
        self.table.setRowCount(0)
        self.apply_button.setEnabled(False)
        self.summary.setText("" if dest.layout else "This library has no layout yet: Apply sets it.")

    def _preview(self) -> None:
        dest = self._dest()
        try:
            self.ops = renamer.preview(
                self.store, self.library_id, self.template.text(), self.untagged_dir.text(), dest.id
            )
        except renamer.TemplateError as error:
            _error(self, str(error))
            return
        source_root = self.store.library(self.library_id).root
        self.table.setRowCount(len(self.ops))
        for row, op in enumerate(self.ops):
            self.table.setItem(row, 0, QTableWidgetItem(str(op.old.relative_to(source_root))))
            self.table.setItem(row, 1, QTableWidgetItem(str(op.new.relative_to(dest.root))))
            problem = QTableWidgetItem(op.conflict or "")
            if op.conflict:
                problem.setForeground(Qt.GlobalColor.red)
            self.table.setItem(row, 2, problem)
        self.table.resizeColumnsToContents()
        ok = sum(op.ok for op in self.ops)
        self.summary.setText(f"{ok} file(s) will move, {len(self.ops) - ok} skipped.")
        # Apply is also how a layout gets saved, even when nothing has to move.
        self.apply_button.setEnabled(True)

    def _apply(self) -> None:
        dest = self._dest()
        try:
            renamer.apply(self.store, self.ops)
        except OSError as error:
            _error(self, f"Move failed, nothing was changed: {error}")
            return
        self.store.set_layout(
            dest.id, self.template.text().strip(), renamer.normalize_untagged_dir(self.untagged_dir.text())
        )
        self.applied = True
        self.ops = []
        self.table.setRowCount(0)
        self.apply_button.setEnabled(False)
        self.summary.setText(f"Done. Layout saved for {dest.name}; it is applied after every scan.")
        self._load_batches()

    def _load_batches(self) -> None:
        self.batches.clear()
        for batch in renamer.batches(self.store):
            when = datetime.fromtimestamp(batch["at"]).strftime("%Y-%m-%d %H:%M")
            state = "undone" if batch["reverted_at"] else "applied"
            item = f"{when} — {batch['n']} file(s) — {state}"
            self.batches.addItem(item)
            self.batches.item(self.batches.count() - 1).setData(Qt.ItemDataRole.UserRole, batch["batch_id"])

    def _undo(self) -> None:
        item = self.batches.currentItem()
        if not item:
            return
        try:
            count = renamer.undo_batch(self.store, item.data(Qt.ItemDataRole.UserRole))
        except OSError as error:
            _error(self, f"Undo failed, nothing was changed: {error}")
            return
        self.applied = True
        self.summary.setText(f"{count} file(s) restored.")
        self._load_batches()


# -------------------------------------------------------------------- settings


class SettingsDialog(QDialog):
    def __init__(self, store: Store, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Settings")
        layout = QFormLayout(self)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.05, 0.99)
        self.threshold.setSingleStep(0.05)
        self.threshold.setValue(float(store.get_setting("suggest_threshold", DEFAULT_THRESHOLD)))
        self.xmp_mode = QComboBox()
        self.xmp_mode.addItems(xmp.MODES)
        self.xmp_mode.setCurrentText(store.get_setting("xmp_mode", "embedded"))
        self.url = QLineEdit(store.get_setting("suggest_url", DEFAULT_URL))
        layout.addRow("Pre-check threshold", self.threshold)
        layout.addRow("XMP writing", self.xmp_mode)
        layout.addRow("Suggestion service", self.url)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _accept(self) -> None:
        self.store.set_setting("suggest_threshold", round(self.threshold.value(), 2))
        self.store.set_setting("xmp_mode", self.xmp_mode.currentText())
        self.store.set_setting("suggest_url", self.url.text().strip() or DEFAULT_URL)
        self.accept()


class ListDialog(QDialog):
    def __init__(self, title: str, lines: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(600, 400)
        layout = QVBoxLayout(self)
        items = QListWidget()
        items.addItems(lines or ["(none)"])
        layout.addWidget(items)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
