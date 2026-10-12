"""XMP keywords: embedded in the image or in a `<file>.xmp` sidecar.

Only three keys are owned by Flicktag; every other metadata field is left as is:
    Xmp.dc.subject               flat tag names
    Xmp.lr.hierarchicalSubject   "category|tag"
    Xmp.flicktag.uid             permanent image id
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import pyexiv2

NAMESPACE = "https://flicktag.local/ns/1.0/"
EMBEDDABLE = {".jpg", ".jpeg", ".png", ".webp"}
MODES = ("embedded", "sidecar", "off")

_EMPTY_PACKET = (
    '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>\n'
    "</x:xmpmeta>\n"
    '<?xpacket end="w"?>\n'
)

# exiv2 keeps global state (namespace registry); serialise all access.
_lock = threading.Lock()
_registered = False


def _ensure_namespace() -> None:
    global _registered
    if not _registered:
        try:
            pyexiv2.registerNs(NAMESPACE, "flicktag")
        except Exception:
            pass  # already registered by an earlier import in this process
        _registered = True


@dataclass(frozen=True)
class Keywords:
    uid: str
    subjects: list[str]
    hierarchical: list[str]


def sidecar_path(path: Path) -> Path:
    return path.with_name(path.name + ".xmp")


def _write_into(target: Path, keywords: Keywords) -> None:
    values = {
        "Xmp.dc.subject": keywords.subjects or None,
        "Xmp.lr.hierarchicalSubject": keywords.hierarchical or None,
        "Xmp.flicktag.uid": keywords.uid,
    }
    with pyexiv2.Image(str(target)) as image:
        current = image.read_xmp()
        # pyexiv2 deletes a key when its value is None; skip deleting absent keys.
        values = {k: v for k, v in values.items() if v is not None or k in current}
        image.modify_xmp(values)


def write(path: Path, keywords: Keywords, mode: str) -> Path | None:
    """Write keywords; return the file that was written, or None in 'off' mode.

    'embedded' falls back to a sidecar for formats exiv2 cannot embed into, or when
    embedding fails.
    """
    if mode == "off":
        return None
    if mode not in MODES:
        raise ValueError(mode)
    with _lock:
        _ensure_namespace()
        if mode == "embedded" and path.suffix.lower() in EMBEDDABLE:
            try:
                _write_into(path, keywords)
                return path
            except Exception:
                pass
        sidecar = sidecar_path(path)
        if not sidecar.exists():
            sidecar.write_text(_EMPTY_PACKET, encoding="utf-8")
        _write_into(sidecar, keywords)
        return sidecar


def read(path: Path) -> dict:
    """Read the XMP of the image, merged over its sidecar (embedded wins)."""
    data: dict = {}
    with _lock:
        _ensure_namespace()
        sidecar = sidecar_path(path)
        for target in (sidecar, path):
            if not target.exists():
                continue
            try:
                with pyexiv2.Image(str(target)) as image:
                    data.update(image.read_xmp())
            except Exception:
                continue
    return data


def read_uid(path: Path) -> str | None:
    uid = read(path).get("Xmp.flicktag.uid")
    return uid if isinstance(uid, str) and uid else None
