# Flicktag

Fast fullscreen tagger for photos and drawing references. One image at a time, one key per
tag, **Space** to validate and move on. A local vision model (SigLIP 2, in a Podman
container) pre-checks tags and learns from your corrections.

Tags are stored in a central SQLite database, written into the images' XMP metadata, and can
optionally drive a clean, undoable rename. A stable JSON export (`docs/FORMAT.md`) feeds
other tools, such as a quick-poses (timed gesture drawing) app.

## Quick start

```bash
make run ARGS=~/Pictures/Refs     # creates .venv (Python 3.12 via uv) and opens the folder
make up                           # optional: start the suggestion container (GPU)
```

Without the container everything works, just without suggestions (red dot in the header).

## Keys

| Key | Action |
|---|---|
| letter / digit | toggle the tag shown with that key (click works too) |
| `Space` | validate (suggestions included) and go to the next image |
| `→` / `←` | next / previous without validating |
| `Delete` | move the file to the system trash |
| `Ctrl+Z` | undo the last validation or trash |
| `N` | new tag (or new category) |
| `Tab` | hide/show the tag bar |
| `F11` / `Esc` / `F1` | toggle fullscreen / leave fullscreen / help |

Dashed yellow chips are **suggestions**: kept on Space unless you press their key. In an
exclusive category (`·1` after its name) choosing a tag replaces the previous one.

The menu bar at the top holds: open folder, switch library, rescan, rename from tags
(File); queue filter — untagged only, review a category or a tag (Queue); vocabulary editor,
settings (pre-check threshold, XMP mode, service URL), unreadable images, retry pending XMP
writes (Tools).

## Suggestions

```bash
make build        # build localhost/flicktag-suggest (downloads torch + CUDA, a few GB)
make up           # run it on 127.0.0.1:8765 with the GPU (CDI: nvidia.com/gpu=all)
make logs / make down / make restart
make up MODEL=google/siglip2-so400m-patch14-384   # bigger model; embeddings are recomputed
```

How it learns:

- **Cold start**: zero-shot. Each tag is compared to its *prompt* (a short English
  description, editable in Vocabulary; default `a photo of <name>`); within a category the
  best match wins.
- **After ~10 positive and 10 negative examples per tag**: a logistic regression on the
  image embeddings takes over for that tag. Every validated image is an example for every
  tag (you saw the whole bar, so an unchecked tag is a negative). Retrained every 25
  validations and at startup.
- Embeddings are cached per image, so a new image pack benefits from everything learned.

## CLI

```bash
flicktag [FOLDER]                     # GUI
flicktag scan FOLDER                  # add/rescan without the GUI
flicktag export --json [-o FILE] [--library NAME]
flicktag --db /path/other.db …        # alternate database
```

## Development

```bash
make test           # pytest, offscreen Qt
make test-service   # integration test against the running container
make install        # uv tool install + desktop entry
```

Layout: `flicktag/core` (no Qt: store, scan, XMP, rename, undo, session state),
`flicktag/suggest` (HTTP client, engine, classifier), `flicktag/ui` (PyQt6),
`suggest-service/` (FastAPI + transformers container). Design: `docs/superpowers/specs/`.
