"""Filesystem- and URL-safe slugs."""

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase ASCII slug: accents stripped, non-alphanumerics collapsed to '-'."""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    ascii_only = ascii_only.encode("ascii", "ignore").decode("ascii").lower()
    return _NON_ALNUM.sub("-", ascii_only).strip("-")
