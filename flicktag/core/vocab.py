"""Categories, tags and their shortcut keys."""

from __future__ import annotations

import string

from flicktag.core.slug import slugify
from flicktag.core.store import Category, Store, Tag

# Letters taken by reserved actions (N = new tag, with or without Shift). Digits
# and the remaining letters are free for tags. A key is stored as the character
# it types: "a" is A, "A" is Shift+A.
RESERVED_KEYS = frozenset("nN")
PLAIN_KEYS = [c for c in string.ascii_lowercase if c not in RESERVED_KEYS] + list("1234567890")
SHIFT_KEYS = [c for c in string.ascii_uppercase if c not in RESERVED_KEYS]
KEY_POOL = PLAIN_KEYS + SHIFT_KEYS


class VocabError(ValueError):
    pass


def valid_key(key: str) -> bool:
    return len(key) == 1 and key in KEY_POOL


def used_keys(store: Store) -> set[str]:
    return {r["key"] for r in store.query("SELECT key FROM tags WHERE key IS NOT NULL")}


def key_label(key: str) -> str:
    """How a key is shown on chips and lists: "A", "1" or "⇧A"."""
    return f"⇧{key}" if key in SHIFT_KEYS else key.upper()


def format_key(key: str | None) -> str:
    """Editable form of a key: "A", "1" or "+A" for Shift+A."""
    if not key:
        return ""
    return f"+{key}" if key in SHIFT_KEYS else key.upper()


def parse_key(text: str) -> str | None:
    """Inverse of format_key; case-insensitive, a leading "+" means Shift."""
    text = text.strip()
    if not text:
        return None
    if text.startswith("+"):
        return text[1:].upper()
    return text.lower()


def suggest_key(name: str, taken: set[str]) -> str | None:
    """First free letter of the name, then the same with Shift, then any free key."""
    letters = slugify(name)
    for char in list(letters) + [c.upper() for c in letters]:
        if char in KEY_POOL and char not in taken:
            return char
    for char in KEY_POOL:
        if char not in taken:
            return char
    return None


def add_category(
    store: Store,
    name: str,
    exclusive: bool = False,
    rename_rank: int | None = None,
) -> Category:
    slug = slugify(name)
    if not slug:
        raise VocabError("category name needs at least one letter or digit")
    if store.query("SELECT 1 FROM categories WHERE slug = ?", (slug,)):
        raise VocabError(f"category '{name}' already exists")
    position = store.query("SELECT COALESCE(MAX(position) + 1, 0) FROM categories")[0][0]
    cur = store.execute(
        "INSERT INTO categories(name, slug, exclusive, position, rename_rank) VALUES (?, ?, ?, ?, ?)",
        (name.strip(), slug, int(exclusive), position, rename_rank),
    )
    return store.category(cur.lastrowid)


def update_category(
    store: Store,
    category_id: int,
    name: str,
    exclusive: bool,
    rename_rank: int | None,
) -> Category:
    slug = slugify(name)
    if not slug:
        raise VocabError("category name needs at least one letter or digit")
    clash = store.query("SELECT id FROM categories WHERE slug = ? AND id != ?", (slug, category_id))
    if clash:
        raise VocabError(f"category '{name}' already exists")
    store.execute(
        "UPDATE categories SET name = ?, slug = ?, exclusive = ?, rename_rank = ? WHERE id = ?",
        (name.strip(), slug, int(exclusive), rename_rank, category_id),
    )
    return store.category(category_id)


def delete_category(store: Store, category_id: int) -> None:
    store.execute("DELETE FROM categories WHERE id = ?", (category_id,))


def find_tag(store: Store, category_id: int, name: str) -> Tag | None:
    rows = store.query(
        "SELECT id FROM tags WHERE category_id = ? AND slug = ?", (category_id, slugify(name))
    )
    return store.tag(rows[0]["id"]) if rows else None


def add_tag(
    store: Store,
    category_id: int,
    name: str,
    prompt: str | None = None,
    key: str | None = None,
) -> Tag:
    slug = slugify(name)
    if not slug:
        raise VocabError("tag name needs at least one letter or digit")
    if find_tag(store, category_id, name):
        raise VocabError(f"tag '{name}' already exists in this category")
    taken = used_keys(store)
    if key is not None:
        if not valid_key(key):
            raise VocabError(f"'{key}' cannot be used as a shortcut")
        if key in taken:
            raise VocabError(f"shortcut '{key}' is already used")
    else:
        key = suggest_key(name, taken)
    position = store.query(
        "SELECT COALESCE(MAX(position) + 1, 0) FROM tags WHERE category_id = ?", (category_id,)
    )[0][0]
    cur = store.execute(
        "INSERT INTO tags(category_id, name, slug, key, prompt, position) VALUES (?, ?, ?, ?, ?, ?)",
        (category_id, name.strip(), slug, key, prompt or None, position),
    )
    return store.tag(cur.lastrowid)


def update_tag(store: Store, tag_id: int, name: str, key: str | None, prompt: str | None) -> Tag:
    tag = store.tag(tag_id)
    slug = slugify(name)
    if not slug:
        raise VocabError("tag name needs at least one letter or digit")
    clash = store.query(
        "SELECT id FROM tags WHERE category_id = ? AND slug = ? AND id != ?",
        (tag.category_id, slug, tag_id),
    )
    if clash:
        raise VocabError(f"tag '{name}' already exists in this category")
    if key:
        if not valid_key(key):
            raise VocabError(f"'{key}' cannot be used as a shortcut")
        if store.query("SELECT 1 FROM tags WHERE key = ? AND id != ?", (key, tag_id)):
            raise VocabError(f"shortcut '{key}' is already used")
    store.execute(
        "UPDATE tags SET name = ?, slug = ?, key = ?, prompt = ? WHERE id = ?",
        (name.strip(), slug, key or None, prompt or None, tag_id),
    )
    return store.tag(tag_id)


def delete_tag(store: Store, tag_id: int) -> None:
    store.execute("DELETE FROM tags WHERE id = ?", (tag_id,))


def move(store: Store, table: str, item_id: int, delta: int) -> None:
    """Swap an item with its neighbour (delta -1 = up, +1 = down) in display order."""
    if table == "categories":
        rows = store.query("SELECT id FROM categories ORDER BY position, id")
    elif table == "tags":
        category_id = store.tag(item_id).category_id
        rows = store.query(
            "SELECT id FROM tags WHERE category_id = ? ORDER BY position, id", (category_id,)
        )
    else:
        raise ValueError(table)
    ids = [r["id"] for r in rows]
    index = ids.index(item_id)
    target = index + delta
    if not 0 <= target < len(ids):
        return
    ids[index], ids[target] = ids[target], ids[index]
    with store.transaction():
        for position, row_id in enumerate(ids):
            store.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (position, row_id))


DEFAULT_VOCABULARY = [
    # (category, exclusive, rename_rank, [(tag, zero-shot prompt[, key])])
    # Tags added after the first release pin their key so older default keys never shift.
    ("Subject", False, 1, [
        ("figure", "a full body photo of a person"),
        ("portrait", "a close-up portrait of a face"),
        ("hand", "a close-up photo of a hand"),
        ("foot", "a close-up photo of a foot"),
        ("animal", "a photo of an animal"),
        ("drapery", "a photo of draped fabric with folds"),
        ("landscape", "a landscape photo with no people", "g"),
        ("mouth", "a close-up of a mouth, lips, teeth or tongue", "z"),
        ("eye", "a close-up of an eye", "1"),
        ("ear", "a close-up of an ear", "2"),
        ("nose", "a close-up of a nose", "3"),
        ("skeleton", "a skull, bones or a skeleton", "S"),
        ("creature", "a fantasy creature, monster or dragon", "C"),
    ]),
    ("Pose", True, 2, [
        ("standing", "a person standing"),
        ("sitting", "a person sitting"),
        ("lying", "a person lying down"),
        ("kneeling", "a person kneeling"),
        ("dynamic", "a person in a dynamic action pose"),
    ]),
    ("View", True, None, [
        ("front", "a person seen from the front"),
        ("profile", "a person seen from the side, profile view"),
        ("back", "a person seen from behind"),
        ("three-quarter", "a person seen in three-quarter view"),
    ]),
    ("Clothing", True, None, [
        ("nude", "a nude figure model"),
        ("clothed", "a clothed person"),
    ]),
    ("Gender", True, None, [
        ("man", "a man, male figure", "9"),
        ("woman", "a woman, female figure", "0"),
    ]),
    ("Scene", False, None, [
        ("mountain", "a landscape with mountains", "m"),
        ("forest", "a landscape with a forest or trees", "j"),
        ("water", "a landscape with a sea, lake or river", "w"),
        ("sky", "a landscape dominated by sky and clouds", "q"),
        ("urban", "a city street or buildings", "v"),
        ("rock", "a landscape with rocks or cliffs", "x"),
    ]),
    ("Medium", True, None, [
        ("photo", "a photograph", "4"),
        ("drawing", "a drawing, painting or digital illustration", "5"),
        ("3d", "a 3D render or digital sculpture", "D"),
    ]),
    ("Style", True, None, [
        ("realistic", "a realistic depiction of a real person", "7"),
        ("stylized", "a stylized anime or cartoon character", "8"),
    ]),
    ("Layout", False, None, [
        ("sheet", "a study sheet with many small drawings on one page", "6"),
    ]),
]


def seed_defaults(store: Store) -> None:
    """Install the starter vocabulary once, on an empty database."""
    if store.categories():
        return
    with store.transaction():
        for name, exclusive, rank, tags in DEFAULT_VOCABULARY:
            category = add_category(store, name, exclusive=exclusive, rename_rank=rank)
            for tag_name, prompt, *key in tags:
                add_tag(store, category.id, tag_name, prompt=prompt, key=key[0] if key else None)
