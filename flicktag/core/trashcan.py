"""Restore files from the freedesktop.org trash.

Moving *to* the trash is done by Qt (QFile.moveToTrash) in the UI layer; this
module only finds a trashed file by its original path and moves it back.
Spec: https://specifications.freedesktop.org/trash-spec/latest/
"""

from __future__ import annotations

import configparser
import os
import shutil
from pathlib import Path
from urllib.parse import unquote


def _mount_point(path: Path) -> Path:
    path = path.resolve() if path.exists() else path.parent.resolve()
    while not os.path.ismount(path):
        path = path.parent
    return path


def trash_dirs(original: Path) -> list[Path]:
    home_trash = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "Trash"
    dirs = [home_trash]
    top = _mount_point(original.parent)
    uid = os.getuid()
    dirs += [top / ".Trash" / str(uid), top / f".Trash-{uid}"]
    return [d for d in dirs if (d / "info").is_dir()]


def _info_path(info_file: Path, trash_dir: Path) -> tuple[Path, str] | None:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(info_file, encoding="utf-8")
        section = parser["Trash Info"]
        raw = unquote(section["Path"])
    except Exception:
        return None
    path = Path(raw)
    if not path.is_absolute():
        # Paths in a top-directory trash are relative to that top directory.
        path = trash_dir.parent.parent / raw if trash_dir.name.isdigit() else trash_dir.parent / raw
    return path, section.get("DeletionDate", "")


def restore(original: Path) -> bool:
    """Move the most recently trashed copy of `original` back. False if not found."""
    original = Path(os.path.abspath(original))
    if original.exists():
        return False
    best: tuple[str, Path, Path] | None = None
    for trash_dir in trash_dirs(original):
        for info_file in (trash_dir / "info").glob("*.trashinfo"):
            parsed = _info_path(info_file, trash_dir)
            if not parsed or Path(os.path.abspath(parsed[0])) != original:
                continue
            stored = trash_dir / "files" / info_file.name[: -len(".trashinfo")]
            if stored.exists() and (best is None or parsed[1] > best[0]):
                best = (parsed[1], info_file, stored)
    if best is None:
        return False
    _, info_file, stored = best
    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(stored), str(original))
    info_file.unlink(missing_ok=True)
    return True
