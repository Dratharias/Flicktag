"""Entry point.

    flicktag [FOLDER]                       open the tagger (optionally on FOLDER)
    flicktag scan FOLDER                    add/rescan a library without the GUI
    flicktag export --json [-o FILE] [--library NAME]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from flicktag.core import export, library, vocab
from flicktag.core.store import Store


def default_db() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "flicktag" / "flicktag.db"


def _open_store(path: str | None) -> Store:
    store = Store(path or default_db())
    vocab.seed_defaults(store)
    return store


def cmd_scan(args) -> int:
    store = _open_store(args.db)
    lib = store.add_library(args.folder)
    report = library.scan(store, lib)
    print(
        f"{lib.name}: {report.added} added, {report.moved} moved, {report.missing} missing, "
        f"{len(report.unreadable)} unreadable"
    )
    return 0


def cmd_export(args) -> int:
    store = _open_store(args.db)
    library_id = None
    if args.library:
        lib = store.library_by_name(args.library)
        if lib is None:
            print(f"unknown library: {args.library}", file=sys.stderr)
            return 2
        library_id = lib.id
    if args.output:
        with open(args.output, "w", encoding="utf-8") as out:
            count = export.write_jsonl(store, out, library_id)
        print(f"{count} images exported to {args.output}", file=sys.stderr)
    else:
        export.write_jsonl(store, sys.stdout, library_id)
    return 0


def cmd_gui(args) -> int:
    from PyQt6.QtWidgets import QApplication

    from flicktag.ui.window import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Flicktag")
    store = _open_store(args.db)
    library_id = None
    if args.folder:
        if not Path(args.folder).is_dir():
            print(f"not a folder: {args.folder}", file=sys.stderr)
            return 2
        library_id = store.add_library(args.folder).id
    window = MainWindow(store, library_id)
    window.showMaximized()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--db")
    _, rest = pre.parse_known_args(argv)
    parser = argparse.ArgumentParser(prog="flicktag", description="Fast fullscreen image tagger")
    parser.add_argument("--db", help=f"database path (default {default_db()})")
    if rest and rest[0] in ("scan", "export"):
        sub = parser.add_subparsers(dest="command", required=True)
        scan = sub.add_parser("scan", help="add or rescan a library")
        scan.add_argument("folder")
        scan.set_defaults(func=cmd_scan)
        exp = sub.add_parser("export", help="export tags as JSON Lines")
        exp.add_argument("--json", action="store_true", help="JSON Lines output (the only format)")
        exp.add_argument("-o", "--output")
        exp.add_argument("--library")
        exp.set_defaults(func=cmd_export)
    else:
        parser.add_argument("folder", nargs="?", help="image folder to open")
        parser.set_defaults(func=cmd_gui)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
