"""Moving files to where the library layout says: preview -> apply (journaled batch) -> undo.

Paths are computed by `core/layout.py`. Files can change folder and library;
sidecars follow, target folders are created and folders left empty are removed.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from flicktag.core import xmp
from flicktag.core.layout import (  # noqa: F401 — re-exported for callers of the old rename API
    DEFAULT_UNTAGGED_DIR,
    MAX_STEM,
    TemplateError,
    default_template,
    normalize_untagged_dir,
    parse_layout,
    parse_template,
    render,
    slugify_stem,
    tag_values,
    tagged_path,
    untagged_path,
)
from flicktag.core.store import Store

MOVABLE = ("tagged", "untagged")


@dataclass(frozen=True)
class RenameOp:
    image_id: int
    old: Path
    new: Path
    conflict: str | None = None  # reason this op cannot be applied
    library_id: int | None = None  # destination library; None = the image's current one

    @property
    def ok(self) -> bool:
        return self.conflict is None


@dataclass(frozen=True)
class TidyReport:
    moved: int
    conflicts: int
    batch_id: str | None


def preview(
    store: Store,
    library_id: int,
    template: str,
    untagged_dir: str = DEFAULT_UNTAGGED_DIR,
    dest_id: int | None = None,
) -> list[RenameOp]:
    """Ops moving the images of `library_id` to where the layout of `dest_id` puts them.

    Only images that are not already in place are listed.
    """
    segments = parse_layout(template, {c.slug for c in store.categories()})
    untagged_dir = normalize_untagged_dir(untagged_dir)
    dest = store.library(dest_id or library_id)
    images = store.images(library_id, statuses=MOVABLE)
    planned = []  # (image, old, new), tagged first so untagged ones avoid their targets
    for image in sorted(images, key=lambda i: i.status != "tagged"):
        if image.status == "tagged":
            rel = tagged_path(segments, tag_values(store, image.id), image.uid, image.rel_path)
        else:
            rel = untagged_path(untagged_dir, image.rel_path, image.uid)
        planned.append((image, store.image_path(image), dest.root / rel))
    moving = {old for _, old, new in planned if new != old}
    claimed = {old for _, old, new in planned if new == old}  # images already in place

    def taken(new: Path, old: Path) -> bool:
        return new in claimed or (new.exists() and new not in moving and not _same_file(new, old))

    ops: list[RenameOp] = []
    for image, old, new in planned:
        if new == old:
            continue
        if image.status == "untagged" and taken(new, old):
            new = dest.root / untagged_path(untagged_dir, image.rel_path, image.uid, with_uid=True)
        conflict = None
        if not old.exists():
            conflict = "file is missing"
        elif new in claimed:
            conflict = "another image gets the same name"
        elif taken(new, old):
            conflict = "a file with this name already exists"
        elif not dest.root.exists():
            conflict = "destination library folder is missing"
        elif _device(old) != _device(dest.root):
            conflict = "destination is on another filesystem"
        claimed.add(new)
        ops.append(RenameOp(image.id, old, new, conflict, dest.id))
    return ops


def _device(path: Path) -> int:
    return os.stat(path).st_dev


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _move_pair(src: Path, dst: Path) -> None:
    os.rename(src, dst)
    sidecar = xmp.sidecar_path(src)
    if sidecar.exists():
        os.rename(sidecar, xmp.sidecar_path(dst))


def _make_parents(path: Path, created: list[Path]) -> None:
    """mkdir -p, remembering every folder it created (outermost first)."""
    missing = []
    folder = path.parent
    while not folder.exists():
        missing.append(folder)
        folder = folder.parent
    for folder in reversed(missing):
        folder.mkdir()
        created.append(folder)


def _remove_if_empty(folders: list[Path]) -> None:
    for folder in sorted(set(folders), key=lambda f: len(f.parts), reverse=True):
        try:
            folder.rmdir()
        except OSError:
            pass  # not empty (or already gone)


def _execute(ops: list[RenameOp], tag: str) -> None:
    """Move files in two phases (source -> temporary -> target) so swaps and chains work.

    On failure every file already moved is put back, folders created for the
    batch are removed, and the error is re-raised.
    """
    staged: list[tuple[RenameOp, Path]] = []
    done: list[RenameOp] = []
    created: list[Path] = []
    try:
        for op in ops:
            temp = op.old.with_name(f".{op.old.name}.flicktag-{tag}")
            _move_pair(op.old, temp)
            staged.append((op, temp))
        for op, temp in staged:
            _make_parents(op.new, created)
            _move_pair(temp, op.new)
            done.append(op)
    except OSError:
        for op, temp in reversed(staged):
            current = op.new if op in done else temp
            if current.exists():
                _move_pair(current, op.old)
        _remove_if_empty(created)
        raise


def _prune(store: Store, ops: list[RenameOp], roots: dict[int, Path]) -> None:
    """Remove folders left empty by `ops`, up to (never including) the library root
    and the untagged folder of the library they were in."""
    keep: set[Path] = set()
    for library_id, root in roots.items():
        keep.add(root)
        if store.has_library(library_id):
            library = store.library(library_id)
            if library.layout:
                keep.add(root / (library.untagged_dir or DEFAULT_UNTAGGED_DIR))
    for op in ops:
        root = next((r for r in roots.values() if op.old.is_relative_to(r)), None)
        folder = op.old.parent
        while root is not None and folder != root and folder.is_relative_to(root) and folder not in keep:
            try:
                folder.rmdir()
            except OSError:
                break  # not empty
            folder = folder.parent


def _commit_paths(store: Store, ops: list[RenameOp], batch_id: str | None) -> None:
    """Point the DB at the new locations (two phases, for the unique rel_path constraint).

    If the DB update fails, the files are moved back so disk and DB stay in sync.
    """
    try:
        with store.transaction():
            before: dict[int, dict] = {}
            for op in ops:
                image = store.image(op.image_id)
                before[op.image_id] = {"library_id": image.library_id, "rel_path": image.rel_path}
                store.update_image(op.image_id, rel_path=f"\0moving/{op.image_id}")
            for op in ops:
                library_id = op.library_id or before[op.image_id]["library_id"]
                new_rel = op.new.relative_to(store.library(library_id).root).as_posix()
                store.update_image(op.image_id, library_id=library_id, rel_path=new_rel)
                if batch_id:
                    store.add_journal(
                        "rename", op.image_id, before[op.image_id], {"library_id": library_id, "rel_path": new_rel},
                        batch_id=batch_id,
                    )
    except Exception:
        _execute([RenameOp(op.image_id, op.new, op.old) for op in ops], "rollback")
        raise


def _roots(store: Store, ops: list[RenameOp]) -> dict[int, Path]:
    ids = {store.image(op.image_id).library_id for op in ops}
    return {i: store.library(i).root for i in ids}


def apply(store: Store, ops: list[RenameOp]) -> str:
    """Apply every ok op as one journaled batch; return the batch id."""
    batch_id = uuid.uuid4().hex
    ops = [op for op in ops if op.ok]
    roots = _roots(store, ops)
    _execute(ops, batch_id[:8])
    _commit_paths(store, ops, batch_id)
    _prune(store, ops, roots)
    return batch_id


def tidy(store: Store, library_id: int) -> TidyReport | None:
    """Move every misplaced image of a library that has a layout; None if it has none."""
    library = store.library(library_id)
    if not library.layout:
        return None
    ops = preview(store, library_id, library.layout, library.untagged_dir or DEFAULT_UNTAGGED_DIR)
    ok = [op for op in ops if op.ok]
    batch_id = apply(store, ok) if ok else None
    return TidyReport(len(ok), len(ops) - len(ok), batch_id)


def batches(store: Store) -> list[dict]:
    """Rename batches, newest first: batch_id, n, at, reverted_at."""
    rows = store.query(
        "SELECT batch_id, COUNT(*) AS n, MIN(at) AS at, MAX(reverted_at) AS reverted_at "
        "FROM journal WHERE kind = 'rename' GROUP BY batch_id ORDER BY at DESC"
    )
    return [dict(r) for r in rows]


def undo_batch(store: Store, batch_id: str) -> int:
    """Move every file of a batch back; return how many were restored.

    Files moved again since the batch, whose library is gone, or whose original
    place is now taken by an unrelated file, are left alone. Journal entries
    written before layouts existed have no library_id and mean "same library".
    """
    rows = store.query(
        "SELECT * FROM journal WHERE batch_id = ? AND kind = 'rename' AND reverted_at IS NULL", (batch_id,)
    )
    candidates: list[RenameOp] = []
    for row in rows:
        image = store.image(row["image_id"])
        before, after = json.loads(row["before"]), json.loads(row["after"])
        before_lib = before.get("library_id", image.library_id)
        after_lib = after.get("library_id", image.library_id)
        if (image.library_id, image.rel_path) != (after_lib, after["rel_path"]) or not store.has_library(before_lib):
            continue
        current = store.library(after_lib).root / after["rel_path"]
        original = store.library(before_lib).root / before["rel_path"]
        if current.exists():
            candidates.append(RenameOp(image.id, current, original, library_id=before_lib))
    sources = {op.old for op in candidates}
    ops = [op for op in candidates if not op.new.exists() or op.new in sources]
    roots = _roots(store, ops)
    _execute(ops, "undo")
    _commit_paths(store, ops, None)
    _prune(store, ops, roots)
    store.mark_reverted([r["id"] for r in rows])
    return len(ops)
