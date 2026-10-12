from pathlib import Path

import pytest
from PIL import Image

from flicktag.core import vocab
from flicktag.core.store import Store


def make_image(path: Path, color=(200, 30, 30), size=(64, 48), fmt=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format=fmt)
    return path


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "flicktag.db")
    yield s
    s.close()


@pytest.fixture
def seeded(store):
    vocab.seed_defaults(store)
    return store


@pytest.fixture
def photos(tmp_path):
    root = tmp_path / "photos"
    make_image(root / "a.jpg", (255, 0, 0))
    make_image(root / "b.png", (0, 255, 0))
    make_image(root / "sub" / "c.webp", (0, 0, 255))
    return root


class FakeTrash:
    """Stands in for QFile.moveToTrash + trashcan.restore."""

    def __init__(self, bin_dir: Path):
        self.bin = bin_dir
        self.bin.mkdir(parents=True, exist_ok=True)
        self.fail = False

    def trash(self, path: str) -> bool:
        if self.fail:
            return False
        src = Path(path)
        src.rename(self.bin / src.name)
        return True

    def restore(self, path: str) -> bool:
        dst = Path(path)
        stored = self.bin / dst.name
        if not stored.exists():
            return False
        stored.rename(dst)
        return True


@pytest.fixture
def fake_trash(tmp_path):
    return FakeTrash(tmp_path / "bin")


def tag_id(store: Store, category_slug: str, tag_slug: str) -> int:
    return store.query(
        "SELECT t.id FROM tags t JOIN categories c ON c.id = t.category_id WHERE c.slug = ? AND t.slug = ?",
        (category_slug, tag_slug),
    )[0]["id"]
