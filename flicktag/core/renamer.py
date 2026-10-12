"""Tag-based renaming: template -> preview -> apply (journaled batch) -> undo.

Template placeholders are category slugs plus {uid}, e.g. "{subject}_{pose}_{uid}".
A category with no tag on the image is dropped together with the separator in
front of it. Files are renamed in place (same directory), sidecars included.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from flicktag.core import xmp
from flicktag.core.slug import slugify
from flicktag.core.store import Store

DEFAULT_TEMPLATE = "{subject}_{pose}_{uid}"
MAX_STEM = 80
_TOKEN = re.compile(r"\{([a-z0-9-]+)\}")


class TemplateError(ValueError):
    pass


def default_template(store: Store) -> str:
    """Categories with a rename rank, in rank order, then {uid}."""
    ranked = sorted((c for c in store.categories() if c.rename_rank), key=lambda c: c.rename_rank)
    return "_".join(["{%s}" % c.slug for c in ranked] + ["{uid}"])


@dataclass(frozen=True)
class RenameOp:
    image_id: int
    old: Path
    new: Path
    conflict: str | None = None  # reason this op cannot be applied

    @property
    def ok(self) -> bool:
        return self.conflict is None


def parse_template(template: str, category_slugs: set[str]) -> list[tuple[str, str]]:
    """Split a template into (literal_before, token) pairs plus a trailing literal."""
    parts: list[tuple[str, str]] = []
    position = 0
    for match in _TOKEN.finditer(template):
        token = match.group(1)
        if token != "uid" and token not in category_slugs:
            raise TemplateError(f"unknown placeholder {{{token}}}")
        parts.append((template[position : match.start()], token))
        position = match.end()
    if "uid" not in [token for _, token in parts]:
        raise TemplateError("the template must contain {uid} so names stay unique")
    trailing = template[position:]
    if trailing:
        parts.append((trailing, ""))
    return parts


def _assemble(parts: list[tuple[str, str]], values: dict[str, str], uid: str) -> str:
    pieces: list[str] = []
    for literal, token in parts:
        if not token:
            pieces.append(literal)
            continue
        value = uid if token == "uid" else values.get(token, "")
        if value:
            pieces.append(value if not pieces else literal + value)
    return "".join(pieces)


def render(parts: list[tuple[str, str]], values: dict[str, str], uid: str) -> str:
    """Fill the template; shorten the longest tag value until the stem fits MAX_STEM."""
    values = dict(values)
    stem = _assemble(parts, values, uid)
    while len(stem) > MAX_STEM:
        longest = max((k for k in values if values[k]), key=lambda k: len(values[k]), default=None)
        if longest is None:
            break
        values[longest] = values[longest][:-1].rstrip("-")
        stem = _assemble(parts, values, uid)
    return stem


def tag_values(store: Store, image_id: int) -> dict[str, str]:
    """Category slug -> tag slug used in the name (first tag by position)."""
    categories = {c.id: c for c in store.categories()}
    values: dict[str, str] = {}
    for tag in store.image_tags(image_id):  # already in category/tag display order
        values.setdefault(categories[tag.category_id].slug, tag.slug)
    return values


def preview(store: Store, library_id: int, template: str) -> list[RenameOp]:
    parts = parse_template(template, {c.slug for c in store.categories()})
    ops: list[RenameOp] = []
    for image in store.images(library_id, statuses=("tagged", "untagged")):
        old = store.image_path(image)
        stem = render(parts, tag_values(store, image.id), image.uid)
        stem = slugify_stem(stem) or image.uid
        new = old.with_name(stem + old.suffix.lower())
        if new.name != old.name:
            ops.append(RenameOp(image.id, old, new))

    sources = {op.old for op in ops}
    targets: dict[Path, int] = {}
    checked: list[RenameOp] = []
    for op in ops:
        conflict = None
        if not op.old.exists():
            conflict = "file is missing"
        elif op.new in targets:
            conflict = "another image gets the same name"
        elif op.new.exists() and op.new not in sources and not _same_file(op.new, op.old):
            conflict = "a file with this name already exists"
        targets[op.new] = op.image_id
        checked.append(RenameOp(op.image_id, op.old, op.new, conflict))
    return checked


def slugify_stem(stem: str) -> str:
    """Clean a rendered stem while keeping the template's '_' separators."""
    return "_".join(filter(None, (slugify(part) for part in stem.split("_"))))


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


def _execute(ops: list[RenameOp], tag: str) -> None:
    """Move files in two phases (source -> temporary -> target) so swaps and chains work.

    On failure every file already moved is put back and the error is re-raised.
    """
    staged: list[tuple[RenameOp, Path]] = []
    done: list[RenameOp] = []
    try:
        for op in ops:
            temp = op.old.with_name(f".{op.old.name}.flicktag-{tag}")
            _move_pair(op.old, temp)
            staged.append((op, temp))
        for op, temp in staged:
            _move_pair(temp, op.new)
            done.append(op)
    except OSError:
        for op, temp in reversed(staged):
            current = op.new if op in done else temp
            if current.exists():
                _move_pair(current, op.old)
        raise


def _commit_paths(store: Store, ops: list[RenameOp], batch_id: str | None) -> None:
    """Point the DB at the new names (two phases, for the unique rel_path constraint).

    If the DB update fails, the files are moved back so disk and DB stay in sync.
    """
    try:
        with store.transaction():
            old_rels: dict[int, str] = {}
            for op in ops:
                old_rels[op.image_id] = store.image(op.image_id).rel_path
                store.update_image(op.image_id, rel_path=f"\0renaming/{op.image_id}")
            for op in ops:
                image = store.image(op.image_id)
                new_rel = op.new.relative_to(store.library(image.library_id).root).as_posix()
                store.update_image(op.image_id, rel_path=new_rel)
                if batch_id:
                    store.add_journal(
                        "rename", op.image_id, {"rel_path": old_rels[op.image_id]}, {"rel_path": new_rel},
                        batch_id=batch_id,
                    )
    except Exception:
        _execute([RenameOp(op.image_id, op.new, op.old) for op in ops], "rollback")
        raise


def apply(store: Store, ops: list[RenameOp]) -> str:
    """Apply every ok op as one journaled batch; return the batch id."""
    batch_id = uuid.uuid4().hex
    ops = [op for op in ops if op.ok]
    _execute(ops, batch_id[:8])
    _commit_paths(store, ops, batch_id)
    return batch_id


def batches(store: Store) -> list[dict]:
    """Rename batches, newest first: batch_id, n, at, reverted_at."""
    rows = store.query(
        "SELECT batch_id, COUNT(*) AS n, MIN(at) AS at, MAX(reverted_at) AS reverted_at "
        "FROM journal WHERE kind = 'rename' GROUP BY batch_id ORDER BY at DESC"
    )
    return [dict(r) for r in rows]


def undo_batch(store: Store, batch_id: str) -> int:
    """Rename every file of a batch back; return how many were restored.

    Files renamed again since the batch, or whose original name is now taken by
    an unrelated file, are left alone.
    """
    rows = store.query(
        "SELECT * FROM journal WHERE batch_id = ? AND kind = 'rename' AND reverted_at IS NULL", (batch_id,)
    )
    candidates: list[RenameOp] = []
    for row in rows:
        image = store.image(row["image_id"])
        after_rel = json.loads(row["after"])["rel_path"]
        root = store.library(image.library_id).root
        current, original = root / after_rel, root / json.loads(row["before"])["rel_path"]
        if image.rel_path == after_rel and current.exists():
            candidates.append(RenameOp(image.id, current, original))
    sources = {op.old for op in candidates}
    ops = [op for op in candidates if not op.new.exists() or op.new in sources]
    _execute(ops, "undo")
    _commit_paths(store, ops, None)
    store.mark_reverted([r["id"] for r in rows])
    return len(ops)
