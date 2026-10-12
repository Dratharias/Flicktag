"""Library layout: where each image belongs on disk, computed from its tags.

A layout is a path template plus a folder for untagged images. The template is
split on "/": every segment but the last is a folder, the last is the file stem
and must contain {uid}. Placeholders are category slugs plus {uid}, e.g.
"{subject}/{pose}/{subject}_{pose}_{uid}". A category with no tag on the image
is dropped together with the separator in front of it, and a folder that
renders empty is skipped. A template without "/" renames in place.

Pure path logic: no file access, no Qt.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from flicktag.core.slug import slugify
from flicktag.core.store import Store

MAX_STEM = 80
DEFAULT_UNTAGGED_DIR = "À trier"
_TOKEN = re.compile(r"\{([a-z0-9-]+)\}")

Parts = list[tuple[str, str]]


class TemplateError(ValueError):
    pass


def default_template(store: Store) -> str:
    """Categories with a rename rank as folders, in rank order, then the same as the stem."""
    ranked = ["{%s}" % c.slug for c in sorted((c for c in store.categories() if c.rename_rank), key=lambda c: c.rename_rank)]
    return "/".join(ranked + ["_".join(ranked + ["{uid}"])])


def parse_template(template: str, category_slugs: set[str], require_uid: bool = True) -> Parts:
    """Split one segment into (literal_before, token) pairs plus a trailing literal."""
    parts: Parts = []
    position = 0
    for match in _TOKEN.finditer(template):
        token = match.group(1)
        if token != "uid" and token not in category_slugs:
            raise TemplateError(f"unknown placeholder {{{token}}}")
        parts.append((template[position : match.start()], token))
        position = match.end()
    if require_uid and "uid" not in [token for _, token in parts]:
        raise TemplateError("the file name must contain {uid} so names stay unique")
    trailing = template[position:]
    if trailing:
        parts.append((trailing, ""))
    return parts


def parse_layout(template: str, category_slugs: set[str]) -> list[Parts]:
    """Parse every segment; the last one (the file stem) must contain {uid}."""
    segments = template.strip().split("/")
    if any(not segment.strip() for segment in segments):
        raise TemplateError("empty folder in the template (leading, trailing or double '/')")
    return [
        parse_template(segment.strip(), category_slugs, require_uid=i == len(segments) - 1)
        for i, segment in enumerate(segments)
    ]


def normalize_untagged_dir(text: str) -> str:
    parts = [p.strip() for p in text.strip().strip("/").split("/")]
    if not text.strip() or text.strip().startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise TemplateError("the untagged folder must be a relative path inside the library")
    return "/".join(parts)


def _assemble(parts: Parts, values: dict[str, str], uid: str) -> str:
    pieces: list[str] = []
    for literal, token in parts:
        if not token:
            pieces.append(literal)
            continue
        value = uid if token == "uid" else values.get(token, "")
        if value:
            pieces.append(value if not pieces else literal + value)
    return "".join(pieces)


def render(parts: Parts, values: dict[str, str], uid: str) -> str:
    """Fill a segment; shorten the longest tag value until it fits MAX_STEM."""
    values = dict(values)
    stem = _assemble(parts, values, uid)
    while len(stem) > MAX_STEM:
        longest = max((k for k in values if values[k]), key=lambda k: len(values[k]), default=None)
        if longest is None:
            break
        values[longest] = values[longest][:-1].rstrip("-")
        stem = _assemble(parts, values, uid)
    return stem


def slugify_stem(stem: str) -> str:
    """Clean a rendered segment while keeping the template's '_' separators."""
    return "_".join(filter(None, (slugify(part) for part in stem.split("_"))))


def tag_values(store: Store, image_id: int) -> dict[str, str]:
    """Category slug -> tag slug used in paths (first tag by position)."""
    categories = {c.id: c for c in store.categories()}
    values: dict[str, str] = {}
    for tag in store.image_tags(image_id):  # already in category/tag display order
        values.setdefault(categories[tag.category_id].slug, tag.slug)
    return values


def tagged_path(segments: list[Parts], values: dict[str, str], uid: str, current_rel: str) -> str:
    """Relative target of a tagged image. One segment = rename inside its current folder."""
    current = PurePosixPath(current_rel)
    stem = slugify_stem(render(segments[-1], values, uid)) or uid
    if len(segments) == 1:
        folder = current.parent
    else:
        folders = [slugify_stem(render(parts, values, uid)) for parts in segments[:-1]]
        folder = PurePosixPath(*[f for f in folders if f]) if any(folders) else PurePosixPath(".")
    return (folder / (stem + current.suffix.lower())).as_posix()


def untagged_path(untagged_dir: str, current_rel: str, uid: str, with_uid: bool = False) -> str:
    """Relative target of an untagged image: original name, uid appended on collision."""
    name = PurePosixPath(current_rel).name
    if with_uid:
        path = PurePosixPath(name)
        name = f"{path.stem}_{uid}{path.suffix}"
    return f"{untagged_dir}/{name}"
