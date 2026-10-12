# Flicktag — Design Spec

- **Date**: 2026-10-11
- **Status**: Draft, awaiting review
- **Scope**: the desktop tagger only. The quick-poses player is a separate, future web app
  backed by a self-hosted DB; Flicktag only has to produce data it can consume.

## 1. Purpose

Flicktag sorts a folder of photos and drawing references as fast as possible: one image
fullscreen, one keypress per tag, `Space` to validate and go to the next one. A local
vision model pre-selects tags; the user only corrects. The tags feed a later app that builds
custom tag-based quick-pose (timed gesture drawing) sessions.

### Success criteria

- Several hundred images tagged in one sitting with no waiting between images.
- Suggestions improve as the user corrects them.
- Tags end up in three exploitable places: the central DB, XMP metadata in the files, and an
  optional clean tag-based rename.
- A documented, stable JSON export the quick-poses app can rely on.
- Nothing is ever destroyed irreversibly: deletion goes to the system trash, every action
  is undoable.

### Non-goals (v1)

- The quick-poses player, any web UI, any sync to the self-hosted DB.
- A generative VLM (Ollama, Qwen-VL). Possible v2 fallback for tags with no examples.
- Moving files across folders during rename; RAW/HEIC support; multi-user.

## 2. Architecture

Two parts in one repository:

```
Flicktag/
├── flicktag/            # Desktop app (Python 3.12, PyQt6, uv)
│   ├── core/            # No Qt dependency; unit-testable
│   │   ├── library.py   #   folder scan, content hash, multiple libraries
│   │   ├── store.py     #   SQLite access + migrations
│   │   ├── vocab.py     #   categories, tags, shortcut keys
│   │   ├── xmp.py       #   XMP read/write (embedded or sidecar)
│   │   ├── renamer.py   #   template → preview → apply → undo
│   │   ├── history.py   #   undo stack (tags, trash, renames)
│   │   └── export.py    #   stable JSON export
│   ├── suggest/         # HTTP client, zero-shot scoring, per-tag classifiers
│   ├── ui/              # Fullscreen viewer, tag bar, dialogs
│   └── __main__.py      # GUI entry point + `export` CLI subcommand
├── suggest-service/     # Podman container: SigLIP 2 embeddings over local HTTP
├── docs/FORMAT.md       # Data contract for the quick-poses app
├── tests/
├── pyproject.toml
└── Makefile             # up/down/restart/logs (container), run, test, install
```

Principles:

- **One central SQLite DB** at `~/.local/share/flicktag/flicktag.db` (XDG data dir) that
  knows several *libraries* (one per folder or purchased image pack). Images are identified
  independently of their path, so knowledge carries over between packs.
- **`core/` never imports Qt.** All business logic is tested headless.
- **The suggestion service is optional.** The app calls it over `http://127.0.0.1:8765`;
  if it is down, the app works normally without suggestions.
- **Embeddings are cached in the DB** — computed once per image, whatever happens to its path.
- **Per-tag classifiers run in the app**, not in the container; the container only embeds.
- **Trash** uses `QFile.moveToTrash` (freedesktop trash, restorable from the file manager).
  This is the one Qt call outside `ui/`; it is wrapped behind a small `trash` function in
  `ui/` that `core/history.py` receives as a callable, so `core/` stays Qt-free.

Conventions follow the sibling `beeref-palette` project: `uv` venv, Python 3.12, PyQt6,
Makefile targets, tests run with `QT_QPA_PLATFORM=offscreen`.

## 3. Sorting screen and keyboard flow

```
┌──────────────────────────────────────────────────────────────────┐
│ Croquis-Pack-03 · 124/860 · IMG_0412.jpg            ● suggest OK │
│                                                                  │
│                    [ image fit to screen ]                       │
│                                                                  │
│ SUBJECT  [F]figure  [M]hand  [A]animal  [D]drapery               │
│ POSE     [S]standing  [C]sitting  ◌[Y]dynamic  [L]lying          │
│ VIEW     [P]profile  ◌[B]back  [O]front  [H]high angle           │
└──────────────────────────────────────────────────────────────────┘
   filled = confirmed   ◌ dashed = suggested (pre-checked)   plain = off
```

- Fullscreen, dark background, image fitted to screen. Tag bar is a bottom overlay, one row
  per category, in category `position` order.
- **One key per tag**, letters and digits only, auto-assigned from the tag name (first free
  letter of the name, then any free letter/digit), editable in settings. Clicking a chip does
  the same as its key.
- Suggested tags are pre-checked with a distinct dashed style; confidence shows on hover.
  Pressing their key unchecks them.
- Categories are **exclusive** (at most one tag; choosing one replaces the previous) or
  **multiple**.

Reserved keys:

| Key | Action |
|---|---|
| `Space` | Validate current state (suggestions included) and go to next — primary gesture |
| `→` / `←` | Next without validating / previous image |
| `Delete` | Move to trash, go to next |
| `Ctrl+Z` | Undo last action (tag validation, trash) |
| `N` | New tag: quick field with category picker and autocomplete |
| `Tab` | Hide/show the tag bar |
| `F11` | Toggle fullscreen |
| `Esc` | Menu: libraries, settings, vocabulary editor, rename, unreadable list |

Queue and flow:

- Queue order: `untagged` images first (by `rel_path`), resuming at the last position per
  library. A filter can restrict the queue to a library, a category or a tag (review mode).
- The next 3 images are decoded and their suggestions computed in background threads, so
  `Space` never waits.
- On validation, for every tag that was suggested: kept → `feedback.label = 1`, unchecked →
  `label = 0`. Tags added manually without suggestion also record `label = 1`; tags in the
  vocabulary that were neither suggested nor chosen record nothing.
- Supported formats in v1: JPG, PNG, WebP, GIF (first frame).

## 4. Data model

SQLite, schema versioned via a `schema_version` table and ordered migrations in `store.py`.

| Table | Columns |
|---|---|
| `libraries` | `id`, `name`, `root_path` (unique), `last_position` |
| `images` | `id`, `uid` (8 hex chars, unique, permanent), `content_hash` (current BLAKE2b-128 hex), `library_id`, `rel_path`, `width`, `height`, `status` (`untagged`/`tagged`/`trashed`/`unreadable`/`missing`), `xmp_pending` (bool), `tagged_at`, `embedding` (BLOB, float16), `embed_model` |
| `categories` | `id`, `name`, `slug` (unique), `exclusive` (bool), `position`, `rename_rank` (nullable; null = not used in rename) |
| `tags` | `id`, `category_id`, `name`, `slug` (unique within category), `key` (unique, nullable), `prompt` (nullable, English text for zero-shot), `position` |
| `image_tags` | `image_id`, `tag_id`, `source` (`manual`/`suggested`), `created_at`; PK (`image_id`, `tag_id`) |
| `feedback` | `image_id`, `tag_id`, `label` (0/1), `created_at`; PK (`image_id`, `tag_id`) — latest wins |
| `journal` | `id`, `batch_id`, `image_id`, `kind` (`tags`/`trash`/`rename`), `before` (JSON), `after` (JSON), `at`, `reverted_at` |

### Image identity

Writing embedded XMP changes the file's bytes, hence its hash. Therefore:

- `uid` = first 8 hex chars of the BLAKE2b hash of the file **at first scan**; on collision,
  extend to 10, 12… chars. Never changes afterwards.
- `uid` is also written into the XMP as `flicktag:uid`.
- After each XMP write, the stored `content_hash` is updated.
- On rescan, a file is matched to a known image by, in order: (`library_id`, `rel_path`) →
  `flicktag:uid` read from its XMP → `content_hash`. Unmatched files are new images; known
  images whose file is gone become `missing` (tags kept).

### Undo

`history.py` keeps an in-memory undo stack backed by `journal` rows:

- tag validation → restore previous `image_tags`, `feedback` and `status`;
- trash → restore the file from the trash (freedesktop `Trash/info` lookup by original path)
  and reset `status`;
- rename batch → undone as a whole from the rename dialog (see §6), not via `Ctrl+Z`.

Undoing a tag validation re-queues the XMP write with the previous tags.

## 5. XMP

Implemented in `xmp.py` with `pyexiv2`.

- Mode (setting): `embedded` (default for JPG/PNG/WebP), `sidecar` (`<file>.xmp`, used
  automatically for GIF and on embed failure), or `off`.
- Fields written:
  - `Xmp.dc.subject` — flat list of tag names;
  - `Xmp.lr.hierarchicalSubject` — `category|tag` entries (understood by digiKam, Lightroom);
  - `Xmp.flicktag.uid` — custom namespace `https://flicktag.local/ns/1.0/`.
- Existing unrelated XMP/EXIF fields are preserved; only these three are rewritten.
- Writes happen in a background worker after each validation. On failure: `xmp_pending = 1`,
  error logged in the journal, retried on next start and from the menu. Sorting never blocks.

## 6. Rename

`renamer.py`, run on demand from the menu, never during sorting.

- Template setting, default `{subject}_{pose}_{uid}`. Placeholders are category slugs plus
  `{uid}`. Categories without a tag on that image are dropped along with their separator.
- An exclusive category renders its tag slug; a multiple category renders its first tag by
  `position`.
- Slug rules: lowercase, accents stripped (Unicode NFKD), non-alphanumerics → `-`, repeated
  separators collapsed; full stem capped at 80 chars (truncate tag parts, never `uid`);
  original extension kept, lowercased.
- Same directory only. Name collisions are impossible across distinct images thanks to `uid`;
  a collision with a foreign file aborts that file and reports it.
- Flow: preview table (before/after, conflicts highlighted) → apply → one journal batch →
  "Undo batch" available from the rename dialog's history list. Sidecar `.xmp` files are
  renamed together with their image.

## 7. Suggestions

### Service (`suggest-service/`)

- FastAPI + `transformers` + SigLIP 2; default model `google/siglip2-base-patch16-224`,
  configurable via env var (e.g. a `so400m` variant fits in 12 GB).
- Listens on `127.0.0.1:8765` only.
- Endpoints:
  - `GET /health` → `{model, device, dim}`;
  - `POST /embed/images` (multipart, batch) → L2-normalised vectors;
  - `POST /embed/texts` (JSON list) → L2-normalised vectors.
- GPU via `podman run --device nvidia.com/gpu=all` (CDI), CPU fallback when CUDA is absent.
  Model weights in a named volume, downloaded once.
- Makefile: `up`, `down`, `restart`, `logs`.

### App side (`flicktag/suggest/`)

- Client with timeout and exponential backoff; status drives the header indicator.
- Embeddings fetched in batches for upcoming images, stored in `images.embedding` along with
  `embed_model`. Changing the model invalidates them (recomputed lazily).
- Per-tag scoring:
  - **zero-shot** while the tag has fewer than 10 positive or 10 negative feedback rows:
    sigmoid of SigLIP's scaled image–text similarity against `prompt` (default
    `"a photo of {name}"`), using the logit scale/bias returned by `/health`;
  - **classifier** once both thresholds are met: L2-regularised logistic regression on
    embeddings (NumPy implementation, no scikit-learn), retrained in a background thread
    every 25 validations.
- Pre-check rule: probability ≥ threshold (default 0.5, setting). In an exclusive category,
  only the highest-scoring tag, and only if above threshold.

## 8. Export and data contract

- `flicktag export --json [--library NAME] [-o FILE]` writes JSON Lines, one object per
  image with `status` in (`tagged`, `untagged`):
  `{"uid", "content_hash", "library", "path", "width", "height", "status", "tags": {"<category>": ["<tag>", ...]}}`
  where `path` is absolute.
- `docs/FORMAT.md` documents: the export format (the official contract, versioned with a
  `"format_version": 1` header line), the XMP fields, the rename convention, and the SQLite
  schema (informational only — not a contract).

## 9. Error handling

| Case | Behaviour |
|---|---|
| Service down | "offline" indicator, backoff retry, manual sorting unaffected |
| Unreadable image | `status = unreadable`, skipped, listed in the menu |
| File missing at rescan | `status = missing`, tags kept |
| Trash fails | Discreet message, state unchanged |
| XMP write fails | `xmp_pending = 1`, retried; sorting continues |
| DB | Each validation is a single transaction |

## 10. Testing

pytest, `QT_QPA_PLATFORM=offscreen`, images generated in temp dirs.

- `core/`: scan + hash + identity matching on rescan; store migrations; vocab key assignment
  and conflicts; XMP round-trip (write → read back, `uid` and unrelated fields preserved);
  renamer slugging, truncation, collisions, batch undo; history undo of validation and trash
  (trash function injected as a fake); export format.
- `suggest/`: logistic regression on synthetic separable vectors; zero-shot/classifier
  switch; pre-check and exclusivity rules; client against a fake HTTP service.
- `ui/`: `pytest-qt` smoke tests — a key sequence on a temp library yields the expected
  DB state.
- Real service: one integration test marked `service`, skipped by default.

## 11. Open points to verify during implementation

- Podman GPU access on Bazzite (`nvidia-container-toolkit` + CDI spec present).
- `pyexiv2` wheel availability for Python 3.12 on x86_64 Linux and WebP/PNG XMP support.
