"""Background work: image decoding, suggestions, XMP writes, retraining.

Each job runs on a QThreadPool and reports back through Qt signals, which are
delivered on the UI thread.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader


class Signals(QObject):
    image_ready = pyqtSignal(int, QImage)
    image_failed = pyqtSignal(int)
    scores_ready = pyqtSignal(int, dict)
    suggest_status = pyqtSignal(bool, str)  # online, message
    job_done = pyqtSignal(str, object)  # job name, result
    job_failed = pyqtSignal(str, str)  # job name, error


class Job(QRunnable):
    def __init__(self, fn: Callable[[], None]):
        super().__init__()
        self.fn = fn
        self.setAutoDelete(True)

    def run(self) -> None:
        self.fn()


class Workers:
    """Owns the thread pools. Decoding and model calls never share a thread, so a
    slow model never delays the next image."""

    def __init__(self):
        self.signals = Signals()
        self.images = QThreadPool()
        self.images.setMaxThreadCount(3)
        self.model = QThreadPool()
        self.model.setMaxThreadCount(1)
        self.io = QThreadPool()
        self.io.setMaxThreadCount(1)

    def decode(self, image_id: int, path: Path) -> None:
        def job() -> None:
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)  # honour EXIF orientation
            image = reader.read()
            if image.isNull():
                self.signals.image_failed.emit(image_id)
            else:
                self.signals.image_ready.emit(image_id, image)

        self.images.start(Job(job))

    def run(self, pool: QThreadPool, name: str, fn: Callable[[], object]) -> None:
        def job() -> None:
            try:
                result = fn()
            except Exception as error:  # reported, never crashes the app
                self.signals.job_failed.emit(name, f"{error}\n{traceback.format_exc()}")
            else:
                self.signals.job_done.emit(name, result)

        pool.start(Job(job))

    def wait(self) -> None:
        for pool in (self.images, self.model, self.io):
            pool.waitForDone()
