"""SQLite persistence: schema, migrations and thin typed accessors.

The store is shared between the UI thread and background workers, so every
public method takes the same re-entrant lock and the connection is opened with
check_same_thread=False.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

STATUSES = ("untagged", "tagged", "trashed", "unreadable", "missing")

MIGRATIONS: list[str] = [
    # 1 — initial schema
    """
    CREATE TABLE libraries (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        root_path TEXT NOT NULL UNIQUE,
        last_position INTEGER
    );
    CREATE TABLE images (
        id INTEGER PRIMARY KEY,
        uid TEXT NOT NULL UNIQUE,
        content_hash TEXT NOT NULL,
        library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
        rel_path TEXT NOT NULL,
        width INTEGER,
        height INTEGER,
        status TEXT NOT NULL DEFAULT 'untagged',
        xmp_pending INTEGER NOT NULL DEFAULT 0,
        tagged_at REAL,
        embedding BLOB,
        embed_model TEXT,
        UNIQUE (library_id, rel_path)
    );
    CREATE INDEX images_hash ON images(content_hash);
    CREATE TABLE categories (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        exclusive INTEGER NOT NULL DEFAULT 0,
        position INTEGER NOT NULL DEFAULT 0,
        rename_rank INTEGER
    );
    CREATE TABLE tags (
        id INTEGER PRIMARY KEY,
        category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        slug TEXT NOT NULL,
        key TEXT UNIQUE,
        prompt TEXT,
        position INTEGER NOT NULL DEFAULT 0,
        UNIQUE (category_id, slug)
    );
    CREATE TABLE image_tags (
        image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        source TEXT NOT NULL DEFAULT 'manual',
        created_at REAL NOT NULL,
        PRIMARY KEY (image_id, tag_id)
    );
    CREATE TABLE feedback (
        image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        label INTEGER NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (image_id, tag_id)
    );
    CREATE TABLE journal (
        id INTEGER PRIMARY KEY,
        batch_id TEXT,
        image_id INTEGER REFERENCES images(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        before TEXT,
        after TEXT,
        at REAL NOT NULL,
        reverted_at REAL
    );
    CREATE INDEX journal_batch ON journal(batch_id);
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 2 — library layout (tag-driven folder template + folder for untagged images)
    """
    ALTER TABLE libraries ADD COLUMN layout TEXT;
    ALTER TABLE libraries ADD COLUMN untagged_dir TEXT;
    """,
]


class LibraryError(ValueError):
    pass


@dataclass(frozen=True)
class Library:
    id: int
    name: str
    root_path: str
    last_position: int | None
    layout: str | None = None  # path template; None = files are never moved automatically
    untagged_dir: str | None = None

    @property
    def root(self) -> Path:
        return Path(self.root_path)


@dataclass(frozen=True)
class Image:
    id: int
    uid: str
    content_hash: str
    library_id: int
    rel_path: str
    width: int | None
    height: int | None
    status: str
    xmp_pending: bool
    tagged_at: float | None
    embed_model: str | None


@dataclass(frozen=True)
class Category:
    id: int
    name: str
    slug: str
    exclusive: bool
    position: int
    rename_rank: int | None


@dataclass(frozen=True)
class Tag:
    id: int
    category_id: int
    name: str
    slug: str
    key: str | None
    prompt: str | None
    position: int


_IMAGE_COLS = (
    "id, uid, content_hash, library_id, rel_path, width, height, status, "
    "xmp_pending, tagged_at, embed_model"
)


def _image(row: sqlite3.Row) -> Image:
    return Image(
        id=row["id"],
        uid=row["uid"],
        content_hash=row["content_hash"],
        library_id=row["library_id"],
        rel_path=row["rel_path"],
        width=row["width"],
        height=row["height"],
        status=row["status"],
        xmp_pending=bool(row["xmp_pending"]),
        tagged_at=row["tagged_at"],
        embed_model=row["embed_model"],
    )


def _category(row: sqlite3.Row) -> Category:
    return Category(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        exclusive=bool(row["exclusive"]),
        position=row["position"],
        rename_rank=row["rename_rank"],
    )


def _tag(row: sqlite3.Row) -> Tag:
    return Tag(
        id=row["id"],
        category_id=row["category_id"],
        name=row["name"],
        slug=row["slug"],
        key=row["key"],
        prompt=row["prompt"],
        position=row["position"],
    )


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # ----------------------------------------------------------------- basics

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block atomically; nested calls join the outer transaction."""
        with self._lock:
            if self._conn.in_transaction:
                yield self._conn
                return
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            current = row[0] if row else 0
            if not row:
                self._conn.execute("INSERT INTO schema_version VALUES (0)")
            for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
                self._conn.executescript("BEGIN;" + script + f"UPDATE schema_version SET version = {version}; COMMIT;")

    @property
    def schema_version(self) -> int:
        return self.query("SELECT version FROM schema_version")[0][0]

    # --------------------------------------------------------------- settings

    def get_setting(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # -------------------------------------------------------------- libraries

    def add_library(self, root: str | Path, name: str | None = None) -> Library:
        """Register a folder. Libraries never overlap: a folder inside a library is
        refused, and libraries inside the new folder are absorbed into it."""
        root = Path(root).expanduser().resolve()
        existing = self.library_by_root(root)
        if existing:
            return existing
        children = []
        for library in self.libraries():
            if root.is_relative_to(library.root):
                raise LibraryError(f"{root} is already part of the library '{library.name}'")
            if library.root.is_relative_to(root):
                children.append(library)
        with self.transaction():
            cur = self.execute(
                "INSERT INTO libraries(name, root_path) VALUES (?, ?)", (name or root.name, str(root))
            )
            library_id = cur.lastrowid
            for child in children:
                prefix = child.root.relative_to(root).as_posix() + "/"
                self.execute(
                    "UPDATE images SET library_id = ?, rel_path = ? || rel_path WHERE library_id = ?",
                    (library_id, prefix, child.id),
                )
                if self.get_setting("current_library") == child.id:
                    self.set_setting("current_library", library_id)
                self.execute("DELETE FROM libraries WHERE id = ?", (child.id,))
        return self.library(library_id)

    def library(self, library_id: int) -> Library:
        row = self.query("SELECT * FROM libraries WHERE id = ?", (library_id,))[0]
        return Library(
            row["id"], row["name"], row["root_path"], row["last_position"], row["layout"], row["untagged_dir"]
        )

    def has_library(self, library_id: int) -> bool:
        return bool(self.query("SELECT 1 FROM libraries WHERE id = ?", (library_id,)))

    def set_layout(self, library_id: int, template: str | None, untagged_dir: str | None) -> None:
        self.execute(
            "UPDATE libraries SET layout = ?, untagged_dir = ? WHERE id = ?", (template, untagged_dir, library_id)
        )

    def library_by_root(self, root: str | Path) -> Library | None:
        rows = self.query("SELECT id FROM libraries WHERE root_path = ?", (str(root),))
        return self.library(rows[0]["id"]) if rows else None

    def library_by_name(self, name: str) -> Library | None:
        rows = self.query("SELECT id FROM libraries WHERE name = ?", (name,))
        return self.library(rows[0]["id"]) if rows else None

    def libraries(self) -> list[Library]:
        return [self.library(r["id"]) for r in self.query("SELECT id FROM libraries ORDER BY name")]

    def set_last_position(self, library_id: int, image_id: int | None) -> None:
        self.execute("UPDATE libraries SET last_position = ? WHERE id = ?", (image_id, library_id))

    # ----------------------------------------------------------------- images

    def image(self, image_id: int) -> Image:
        return _image(self.query(f"SELECT {_IMAGE_COLS} FROM images WHERE id = ?", (image_id,))[0])

    def images(self, library_id: int | None = None, statuses: tuple[str, ...] | None = None) -> list[Image]:
        sql = f"SELECT {_IMAGE_COLS} FROM images WHERE 1=1"
        params: list[Any] = []
        if library_id is not None:
            sql += " AND library_id = ?"
            params.append(library_id)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        sql += " ORDER BY rel_path"
        return [_image(r) for r in self.query(sql, tuple(params))]

    def image_path(self, image: Image | int) -> Path:
        if isinstance(image, int):
            image = self.image(image)
        return self.library(image.library_id).root / image.rel_path

    def update_image(self, image_id: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE images SET {assignments} WHERE id = ?", (*fields.values(), image_id))

    def get_embedding(self, image_id: int) -> tuple[bytes | None, str | None]:
        row = self.query("SELECT embedding, embed_model FROM images WHERE id = ?", (image_id,))[0]
        return row["embedding"], row["embed_model"]

    # ------------------------------------------------------------- vocabulary

    def categories(self) -> list[Category]:
        return [_category(r) for r in self.query("SELECT * FROM categories ORDER BY position, id")]

    def category(self, category_id: int) -> Category:
        return _category(self.query("SELECT * FROM categories WHERE id = ?", (category_id,))[0])

    def tags(self, category_id: int | None = None) -> list[Tag]:
        if category_id is None:
            rows = self.query(
                "SELECT t.* FROM tags t JOIN categories c ON c.id = t.category_id "
                "ORDER BY c.position, c.id, t.position, t.id"
            )
        else:
            rows = self.query("SELECT * FROM tags WHERE category_id = ? ORDER BY position, id", (category_id,))
        return [_tag(r) for r in rows]

    def tag(self, tag_id: int) -> Tag:
        return _tag(self.query("SELECT * FROM tags WHERE id = ?", (tag_id,))[0])

    # ------------------------------------------------------------ assignments

    def image_tag_ids(self, image_id: int) -> set[int]:
        return {r["tag_id"] for r in self.query("SELECT tag_id FROM image_tags WHERE image_id = ?", (image_id,))}

    def image_tags(self, image_id: int) -> list[Tag]:
        rows = self.query(
            "SELECT t.* FROM image_tags it JOIN tags t ON t.id = it.tag_id "
            "JOIN categories c ON c.id = t.category_id WHERE it.image_id = ? "
            "ORDER BY c.position, c.id, t.position, t.id",
            (image_id,),
        )
        return [_tag(r) for r in rows]

    # ---------------------------------------------------------------- journal

    def add_journal(
        self,
        kind: str,
        image_id: int | None,
        before: Any,
        after: Any,
        batch_id: str | None = None,
    ) -> int:
        cur = self.execute(
            "INSERT INTO journal(batch_id, image_id, kind, before, after, at) VALUES (?, ?, ?, ?, ?, ?)",
            (batch_id, image_id, kind, json.dumps(before), json.dumps(after), time.time()),
        )
        return cur.lastrowid

    def journal_entry(self, entry_id: int) -> sqlite3.Row:
        return self.query("SELECT * FROM journal WHERE id = ?", (entry_id,))[0]

    def mark_reverted(self, entry_ids: list[int]) -> None:
        now = time.time()
        for entry_id in entry_ids:
            self.execute("UPDATE journal SET reverted_at = ? WHERE id = ?", (now, entry_id))
