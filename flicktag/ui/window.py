"""Fullscreen sorting window."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QFile, QPoint, QSize, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QFont, QImage, QKeyEvent, QPainter, QPixmap
from PyQt6.QtWidgets import QFileDialog, QLabel, QMainWindow, QMenu, QWidget

from flicktag.core import library as library_mod
from flicktag.core import renamer, vocab, xmpsync
from flicktag.core.history import History, HistoryError
from flicktag.core.queue import QueueFilter
from flicktag.core.session import Session
from flicktag.core.store import LibraryError, Store
from flicktag.suggest.client import DEFAULT_URL, Offline, SuggestClient
from flicktag.suggest.engine import SuggestionEngine
from flicktag.ui.dialogs import ListDialog, NewTagDialog, OrganizeDialog, SettingsDialog, VocabularyDialog
from flicktag.ui.tagbar import TagBar
from flicktag.ui.workers import Job, Workers

PREFETCH = 4
CACHE_SIZE = 8

HELP = """<b>Flicktag</b><br><br>
<b>Letter / digit</b> toggle tag (⇧ = with Shift) &nbsp;·&nbsp; click a chip does the same<br>
<b>Space / Enter</b> validate &amp; next &nbsp;·&nbsp; <b>→ / ←</b> next / previous without validating<br>
<b>Delete</b> move to trash &nbsp;·&nbsp; <b>Ctrl+Z</b> undo<br>
<b>N</b> new tag &nbsp;·&nbsp; <b>Tab</b> hide tag bar &nbsp;·&nbsp; <b>F11</b> fullscreen<br>
<b>Esc</b> leave fullscreen &nbsp;·&nbsp; <b>F1</b> this help<br><br>
Dashed chips are suggestions: they are kept unless you press their key."""


def move_to_trash(path: str) -> bool:
    """freedesktop.org trash via Qt; PyQt6 returns (ok, path_in_trash)."""
    result = QFile.moveToTrash(path)
    return bool(result[0] if isinstance(result, tuple) else result)


class Canvas(QWidget):
    """Draws the current image fitted to the window on a dark background."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._image: QImage | None = None
        self._scaled: QPixmap | None = None
        self.message = ""

    def set_image(self, image: QImage | None, message: str = "") -> None:
        self._image = image
        self._scaled = None
        self.message = message
        self.update()

    def resizeEvent(self, event) -> None:
        self._scaled = None
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0b0b0d"))
        if self._image is not None and not self._image.isNull():
            if self._scaled is None or self._scaled.size() != self._fit(self._image.size()):
                self._scaled = QPixmap.fromImage(
                    self._image.scaled(
                        self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                    )
                )
            x = (self.width() - self._scaled.width()) // 2
            y = (self.height() - self._scaled.height()) // 2
            painter.drawPixmap(QPoint(x, y), self._scaled)
        if self.message:
            painter.setPen(QColor("#8a8a95"))
            font = QFont(self.font())
            font.setPointSize(18)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)

    def _fit(self, size: QSize) -> QSize:
        return size.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)


class MainWindow(QMainWindow):
    def __init__(self, store: Store, library_id: int | None = None, trash_fn=None):
        super().__init__()
        self.store = store
        self.setWindowTitle("Flicktag")
        self.resize(1400, 900)
        self.workers = Workers()
        self.client = SuggestClient(store.get_setting("suggest_url", DEFAULT_URL))
        self.engine = SuggestionEngine(store, self.client)
        self.history = History(store, trash_fn or move_to_trash)
        self.session: Session | None = None
        self.filter = QueueFilter()
        self._cache: dict[int, QImage] = {}
        self._decoding: set[int] = set()
        self._scoring: set[int] = set()
        self._unreadable_now: set[int] = set()
        self.suggest_online: bool | None = None
        self.suggest_message = "suggestions: connecting…"

        self.canvas = Canvas(self)
        self.setCentralWidget(self.canvas)
        self._build_menubar()
        self.header = QLabel(self.canvas)
        self.header.setStyleSheet(
            "color: #c9c9d1; background: rgba(10,10,12,170); padding: 6px 12px; border-radius: 8px;"
        )
        self.tagbar = TagBar(self.canvas)
        self.tagbar.toggled.connect(self._toggle)
        self.toast = QLabel(self.canvas)
        self.toast.setStyleSheet(
            "color: #111; background: #f2c14e; padding: 8px 16px; border-radius: 8px; font-weight: 600;"
        )
        self.toast.hide()
        self._toast_timer = QTimer(self, singleShot=True, interval=2600, timeout=self.toast.hide)
        self.help = QLabel(HELP, self.canvas)
        self.help.setStyleSheet("color: #ddd; background: rgba(10,10,12,225); padding: 24px; border-radius: 12px;")
        self.help.hide()

        signals = self.workers.signals
        signals.image_ready.connect(self._image_ready)
        signals.image_failed.connect(self._image_failed)
        signals.scores_ready.connect(self._scores_ready)
        signals.suggest_status.connect(self._suggest_status)
        signals.job_done.connect(self._job_done)
        signals.job_failed.connect(self._job_failed)

        self._rebuild_tagbar()
        self.workers.run(self.workers.io, "xmp-retry", lambda: xmpsync.retry_pending(self.store))
        self._retrain()
        self._ping_timer = QTimer(self, interval=15000, timeout=self._ping)
        self._ping_timer.start()
        self._ping()

        library_id = library_id or store.get_setting("current_library")
        if library_id and any(lib.id == library_id for lib in store.libraries()):
            self.open_library(library_id)
        else:
            self.canvas.set_image(None, "No library yet — File → Open folder…")
        self._layout_overlays()

    # --------------------------------------------------------------- libraries

    def open_library(self, library_id: int, rescan: bool = True) -> None:
        self.store.set_setting("current_library", library_id)
        self._cache.clear()
        self.session = Session(self.store, library_id, self.history, self.engine.preselect, self.filter)
        self.session.on_validated = self._validated
        self._refresh()
        if rescan:
            library = self.store.library(library_id)
            self.show_toast(f"Scanning {library.name}…")

            def job():
                report = library_mod.scan(self.store, library)
                return report, renamer.tidy(self.store, library.id)

            self.workers.run(self.workers.io, "scan", job)

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Open image folder", str(Path.home()))
        if folder:
            try:
                library = self.store.add_library(folder)
            except LibraryError as error:
                self.show_toast(str(error))
                return
            self.open_library(library.id)

    # ---------------------------------------------------------------- display

    def _layout_overlays(self) -> None:
        width = self.canvas.width()
        self.header.adjustSize()
        self.header.move(16, 14)
        self.tagbar.setFixedWidth(max(width - 32, 200))
        self.tagbar.adjustSize()
        self.tagbar.move(16, self.canvas.height() - self.tagbar.height() - 16)
        self.toast.adjustSize()
        self.toast.move((width - self.toast.width()) // 2, 18)
        self.help.adjustSize()
        self.help.move((width - self.help.width()) // 2, (self.canvas.height() - self.help.height()) // 2)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_overlays()

    def show_toast(self, text: str) -> None:
        self.toast.setText(text)
        self.toast.show()
        self.toast.raise_()
        self._layout_overlays()
        self._toast_timer.start()

    def _rebuild_tagbar(self) -> None:
        self.tagbar.rebuild(self.store.categories(), self.store.tags)
        self._layout_overlays()

    def _refresh(self) -> None:
        session = self.session
        if session is None:
            return
        image_id = session.current_id
        if image_id is None:
            message = "All done" if session.queue else "Nothing to sort here"
            self.canvas.set_image(None, message)
        elif image_id in self._cache:
            self.canvas.set_image(self._cache[image_id])
        elif image_id in self._unreadable_now:
            self.canvas.set_image(None, "Cannot display this image — → to skip")
        else:
            self.canvas.set_image(None, "Loading…")
        self.tagbar.update_states(session.tag_state, session.scores())
        self._update_header()
        self._prefetch()

    def _update_header(self) -> None:
        session = self.session
        library = self.store.library(session.library_id)
        parts = [library.name]
        if self.filter.kind != "all":
            parts.append(self.filter.label(self.store))
        if session.current_id is not None:
            image = self.store.image(session.current_id)
            parts.append(f"{session.index + 1}/{len(session.queue)}")
            parts.append(f"{session.remaining} untagged left")
            parts.append(Path(image.rel_path).name + ("  ✓" if image.status == "tagged" else ""))
        dot = {True: "<span style='color:#7bd88f'>●</span>", False: "<span style='color:#e06c75'>●</span>"}.get(
            self.suggest_online, "<span style='color:#888'>●</span>"
        )
        self.header.setText(" · ".join(parts) + f" &nbsp; {dot} {self.suggest_message}")
        self._layout_overlays()

    # --------------------------------------------------------------- prefetch

    def _prefetch(self) -> None:
        upcoming = self.session.upcoming(PREFETCH)
        for image_id in upcoming:
            if image_id not in self._cache and image_id not in self._decoding and image_id not in self._unreadable_now:
                self._decoding.add(image_id)
                self.workers.decode(image_id, self.store.image_path(image_id))
        for image_id in list(self._cache):
            if image_id not in upcoming and len(self._cache) > CACHE_SIZE:
                del self._cache[image_id]
        self._request_scores(upcoming)

    def _request_scores(self, image_ids: list[int]) -> None:
        wanted = [i for i in image_ids if not self.session.scores(i) and i not in self._scoring]
        if not wanted:
            return
        self._scoring.update(wanted)

        def job() -> None:
            signals = self.workers.signals
            try:
                self.engine.ensure_embeddings(wanted)
                for image_id in wanted:
                    signals.scores_ready.emit(image_id, self.engine.scores(image_id))
                info = self.client.info()
                signals.suggest_status.emit(True, self._status_text(info.device))
            except Offline as error:
                signals.suggest_status.emit(False, "suggestions offline")
                signals.job_failed.emit("scores-offline", str(error))
            finally:
                signals.job_done.emit("scores", wanted)  # release on the UI thread

        self.workers.model.start(Job(job))

    def _status_text(self, device: str) -> str:
        trained = len(self.engine.trained_tags())
        learned = f", {trained} tag(s) learned" if trained else ", zero-shot"
        return f"suggestions on ({device}{learned})"

    def _ping(self) -> None:
        if self.suggest_online:
            return

        def job() -> None:
            try:
                info = self.client.info()
                self.workers.signals.suggest_status.emit(True, self._status_text(info.device))
            except Offline:
                self.workers.signals.suggest_status.emit(False, "suggestions offline (make up)")

        self.workers.model.start(Job(job))

    # ---------------------------------------------------------------- signals

    def _image_ready(self, image_id: int, image: QImage) -> None:
        self._decoding.discard(image_id)
        self._cache[image_id] = image
        if self.session and image_id == self.session.current_id:
            self.canvas.set_image(image)

    def _image_failed(self, image_id: int) -> None:
        self._decoding.discard(image_id)
        self._unreadable_now.add(image_id)
        if self.session and image_id == self.session.current_id:
            self.canvas.set_image(None, "Cannot display this image — → to skip")

    def _scores_ready(self, image_id: int, scores: dict) -> None:
        if self.session is None:
            return
        self.session.set_scores(image_id, scores)
        if image_id == self.session.current_id:
            self.tagbar.update_states(self.session.tag_state, self.session.scores())

    def _suggest_status(self, online: bool, message: str) -> None:
        was_online = self.suggest_online
        self.suggest_online = online
        self.suggest_message = message
        if self.session:
            self._update_header()
            if online and not was_online:
                self._request_scores(self.session.upcoming(PREFETCH))

    def _job_done(self, name: str, result: object) -> None:
        if name == "scores":
            self._scoring.difference_update(result)
        elif name == "scan" and self.session:
            report, tidied = result
            if tidied and tidied.moved:
                self._cache.clear()
                self._unreadable_now.clear()
            self.session.refresh_queue()
            self._refresh()
            text = f"Scan: {report.added} new"
            if report.moved:
                text += f", {report.moved} moved"
            if report.missing:
                text += f", {report.missing} missing"
            if report.unreadable:
                text += f", {len(report.unreadable)} unreadable"
            if tidied and (tidied.moved or tidied.conflicts):
                text += f" · Tidy: {tidied.moved} moved"
                if tidied.conflicts:
                    text += f", {tidied.conflicts} conflict(s)"
            self.show_toast(text)
        elif name == "retrain" and self.suggest_online:
            self._ping_status()
        elif name == "xmp-retry":
            ok, failed = result
            if failed:
                self.show_toast(f"XMP: {failed} file(s) still pending")

    def _job_failed(self, name: str, error: str) -> None:
        if name in ("scores-offline", "retrain"):
            return  # surfaced through the status indicator
        first_line = error.splitlines()[0] if error else "unknown error"
        self.show_toast(f"{name} failed: {first_line}")

    def _ping_status(self) -> None:
        info = self.client.cached_info
        if info and self.session:
            self.suggest_message = self._status_text(info.device)
            self._update_header()

    # ---------------------------------------------------------------- actions

    def _validated(self, image_id: int) -> None:
        self.workers.run(self.workers.io, "xmp", lambda: xmpsync.sync(self.store, image_id))
        if self.engine.note_validation():
            self._retrain()

    def _retrain(self) -> None:
        self.workers.run(self.workers.model, "retrain", self.engine.retrain)

    def _toggle(self, tag_id: int) -> None:
        if self.session:
            self.session.toggle(tag_id)
            self.tagbar.update_states(self.session.tag_state, self.session.scores())

    def _new_tag(self) -> None:
        if not self.session or self.session.current_id is None:
            return
        dialog = NewTagDialog(self.store, self)
        if dialog.exec() and dialog.result_tag:
            self._rebuild_tagbar()
            if dialog.result_tag.id not in self.session.checked:
                self.session.toggle(dialog.result_tag.id)
            self.tagbar.update_states(self.session.tag_state, self.session.scores())

    def _undo(self) -> None:
        try:
            image_id = self.session.undo()
        except HistoryError as error:
            self.show_toast(str(error))
            return
        if image_id is None:
            self.show_toast("Nothing to undo")
            return
        self._refresh()
        self.show_toast("Undone")

    def _trash(self) -> None:
        try:
            self.session.trash()
        except HistoryError as error:
            self.show_toast(str(error))
            return
        self._refresh()
        self.show_toast("Moved to trash — Ctrl+Z to restore")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        modifiers = event.modifiers()
        if self.help.isVisible() and key in (Qt.Key.Key_Escape, Qt.Key.Key_F1):
            self.help.hide()
            return
        if key == Qt.Key.Key_Escape:
            if self.isFullScreen():
                self.setWindowState(self.windowState() & ~Qt.WindowState.WindowFullScreen)
            return
        if key == Qt.Key.Key_F11:
            # XOR keeps the maximized flag, so leaving fullscreen goes back to maximized
            self.setWindowState(self.windowState() ^ Qt.WindowState.WindowFullScreen)
            return
        if key == Qt.Key.Key_F1:
            self.help.show()
            self.help.raise_()
            self._layout_overlays()
            return
        if self.session is None:
            return
        if key == Qt.Key.Key_Z and modifiers & Qt.KeyboardModifier.ControlModifier:
            self._undo()
        elif modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            super().keyPressEvent(event)
        elif key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.session.validate_and_next()
            self._refresh()
        elif key == Qt.Key.Key_Right:
            self.session.next()
            self._refresh()
        elif key == Qt.Key.Key_Left:
            self.session.prev()
            self._refresh()
        elif key == Qt.Key.Key_Delete:
            self._trash()
        elif key == Qt.Key.Key_Tab:
            self.tagbar.setVisible(not self.tagbar.isVisible())
        elif key == Qt.Key.Key_N:
            self._new_tag()
        else:
            # Case comes from the Shift modifier, not the text, so Caps Lock is ignored.
            text = event.text().lower()
            if text.isalpha() and modifiers & Qt.KeyboardModifier.ShiftModifier:
                text = text.upper()
            if len(text) == 1 and vocab.valid_key(text):
                tag = next((t for t in self.store.tags() if t.key == text), None)
                if tag:
                    self._toggle(tag.id)
                    return
            super().keyPressEvent(event)

    def focusNextPrevChild(self, next: bool) -> bool:
        return False  # keep Tab for hiding the tag bar

    # ------------------------------------------------------------------- menu

    def _build_menubar(self) -> None:
        bar = self.menuBar()
        bar.setNativeMenuBar(False)  # keep it in the window, not in a desktop global menu
        bar.setStyleSheet(
            "QMenuBar { background: #141417; color: #c9c9d1; padding: 0; }"
            "QMenuBar::item { padding: 2px 10px; background: transparent; }"
            "QMenuBar::item:selected { background: #2a2a30; }"
        )
        # Menus list libraries, categories and tags, so they are refilled each time they open.
        for title, fill in (
            ("&File", self._fill_file_menu),
            ("&Queue", self._fill_queue_menu),
            ("&Tools", self._fill_tools_menu),
            ("&Help", self._fill_help_menu),
        ):
            menu = bar.addMenu(title)
            menu.aboutToShow.connect(lambda menu=menu, fill=fill: (menu.clear(), fill(menu)))

    def _fill_file_menu(self, menu: QMenu) -> None:
        menu.addAction("Open folder…", self.choose_folder)
        libraries = menu.addMenu("Switch library")
        for library in self.store.libraries():
            action = libraries.addAction(library.name, lambda lib=library.id: self.open_library(lib))
            action.setCheckable(True)
            action.setChecked(bool(self.session) and self.session.library_id == library.id)
        if self.session:
            menu.addAction("Rescan library", lambda: self.open_library(self.session.library_id))
            menu.addAction("Organize into…", self._organize)
            menu.addAction("Tidy library", lambda: self._organize(tidy=True))
        menu.addSeparator()
        menu.addAction("Quit", self.close)

    def _fill_queue_menu(self, menu: QMenu) -> None:
        if not self.session:
            menu.addAction("No library open").setEnabled(False)
            return
        self._filter_action(menu, "All (untagged first)", QueueFilter())
        self._filter_action(menu, "Untagged only", QueueFilter("untagged"))
        by_category = menu.addMenu("Review category")
        for category in self.store.categories():
            self._filter_action(by_category, category.name, QueueFilter("category", category.id))
        by_tag = menu.addMenu("Review tag")
        for category in self.store.categories():
            sub = by_tag.addMenu(category.name)
            for tag in self.store.tags(category.id):
                self._filter_action(sub, tag.name, QueueFilter("tag", tag.id))

    def _fill_tools_menu(self, menu: QMenu) -> None:
        menu.addAction("Vocabulary…", self._vocabulary)
        menu.addAction("Settings…", self._settings)
        menu.addSeparator()
        if self.session:
            menu.addAction("Unreadable images…", self._unreadable)
        menu.addAction("Retry pending XMP writes", self._retry_xmp)

    def _fill_help_menu(self, menu: QMenu) -> None:
        menu.addAction("Keys (F1)", lambda: (self.help.show(), self.help.raise_(), self._layout_overlays()))

    def _filter_action(self, menu: QMenu, label: str, queue_filter: QueueFilter) -> QAction:
        action = menu.addAction(label, lambda: self._set_filter(queue_filter))
        action.setCheckable(True)
        action.setChecked(self.filter == queue_filter)
        return action

    def _set_filter(self, queue_filter: QueueFilter) -> None:
        self.filter = queue_filter
        if self.session:
            self.open_library(self.session.library_id, rescan=False)

    def _vocabulary(self) -> None:
        dialog = VocabularyDialog(self.store, self)
        dialog.exec()
        if dialog.changed:
            self._rebuild_tagbar()
            if self.session:
                self.session.refresh_queue()
                self._refresh()

    def _organize(self, tidy: bool = False) -> None:
        dialog = OrganizeDialog(self.store, self.session.library_id, tidy, self)
        dialog.exec()
        if dialog.applied:
            self._cache.clear()
            self._unreadable_now.clear()
            self.session.refresh_queue()
            self._refresh()

    def _settings(self) -> None:
        dialog = SettingsDialog(self.store, self)
        if dialog.exec():
            url = self.store.get_setting("suggest_url", DEFAULT_URL)
            if url != self.client.base_url:
                self.client = SuggestClient(url)
                self.engine = SuggestionEngine(self.store, self.client)
                if self.session:
                    self.session.preselect = self.engine.preselect
                self.suggest_online = None
                self._ping()
                self._retrain()

    def _unreadable(self) -> None:
        images = self.store.images(self.session.library_id, statuses=("unreadable",))
        ListDialog("Unreadable images", [i.rel_path for i in images], self).exec()

    def _retry_xmp(self) -> None:
        self.workers.run(self.workers.io, "xmp-retry", lambda: xmpsync.retry_pending(self.store))
        self.show_toast("Retrying pending XMP writes…")

    def closeEvent(self, event) -> None:
        self._ping_timer.stop()
        self.workers.wait()
        self.client.close()
        super().closeEvent(event)

