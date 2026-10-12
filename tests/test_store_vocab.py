import pytest

from flicktag.core import vocab
from flicktag.core.slug import slugify
from flicktag.core.store import MIGRATIONS, Store


def test_migrations_applied_once(tmp_path):
    path = tmp_path / "db.sqlite"
    Store(path).close()
    again = Store(path)
    assert again.schema_version == len(MIGRATIONS)
    again.close()


def test_settings_roundtrip(store):
    assert store.get_setting("missing", 3) == 3
    store.set_setting("threshold", 0.7)
    store.set_setting("threshold", 0.4)
    assert store.get_setting("threshold") == 0.4


@pytest.mark.parametrize(
    "text, expected",
    [("Trois-Quarts", "trois-quarts"), ("Élan  dynamique!", "elan-dynamique"), ("  ", ""), ("Plongée/Vue", "plongee-vue")],
)
def test_slugify(text, expected):
    assert slugify(text) == expected


def test_seed_defaults_is_idempotent(store):
    vocab.seed_defaults(store)
    count = len(store.tags())
    vocab.seed_defaults(store)
    assert len(store.tags()) == count > 0


def test_seeded_keys_are_unique_and_never_reserved(seeded):
    keys = [t.key for t in seeded.tags()]
    assert all(keys)
    assert len(keys) == len(set(keys))
    assert not set(keys) & vocab.RESERVED_KEYS


def test_key_prefers_letters_of_the_name(store):
    cat = vocab.add_category(store, "Pose", exclusive=True)
    assert vocab.add_tag(store, cat.id, "standing").key == "s"
    assert vocab.add_tag(store, cat.id, "sitting").key == "i"


def test_reserved_and_duplicate_keys_rejected(store):
    cat = vocab.add_category(store, "Pose")
    vocab.add_tag(store, cat.id, "standing", key="s")
    with pytest.raises(vocab.VocabError):
        vocab.add_tag(store, cat.id, "seated", key="s")
    with pytest.raises(vocab.VocabError):
        vocab.add_tag(store, cat.id, "new", key="n")


def test_key_pool_exhaustion_leaves_tag_without_key(store):
    cat = vocab.add_category(store, "Many")
    for i in range(len(vocab.KEY_POOL)):
        vocab.add_tag(store, cat.id, f"t{i}")
    extra = vocab.add_tag(store, cat.id, "overflow")
    assert extra.key is None


def test_duplicate_tag_and_category_rejected(store):
    cat = vocab.add_category(store, "Pose")
    vocab.add_tag(store, cat.id, "Debout")
    with pytest.raises(vocab.VocabError):
        vocab.add_tag(store, cat.id, "debout")
    with pytest.raises(vocab.VocabError):
        vocab.add_category(store, "pose")
    with pytest.raises(vocab.VocabError):
        vocab.add_tag(store, cat.id, "!!!")


def test_move_reorders(store):
    a = vocab.add_category(store, "A")
    b = vocab.add_category(store, "B")
    vocab.move(store, "categories", b.id, -1)
    assert [c.name for c in store.categories()] == ["B", "A"]
    vocab.move(store, "categories", b.id, -1)  # already first: no-op
    assert [c.name for c in store.categories()] == ["B", "A"]
    assert a.id
