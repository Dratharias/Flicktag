# Flicktag

Fullscreen image tagger (PyQt6) with local SigLIP 2 suggestions in a Podman container.

- Python 3.12 via uv: `make test`, `make run ARGS=<folder>`, `make install`; container: `make build/up/down/logs`.
- `flicktag/core/` must never import Qt (the only Qt trash call is injected from `ui/window.py`).
- `docs/FORMAT.md` is the contract for the quick-poses app: bump `format_version` on breaking export changes.
- Schema changes: append a migration to `MIGRATIONS` in `core/store.py`, never edit an existing one.
- Spec: `docs/superpowers/specs/2026-10-11-flicktag-design.md`.
- Remote `origin` = GitHub `Dratharias/Flicktag`.
