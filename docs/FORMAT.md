# Flicktag data format

This document is the contract between Flicktag (the tagger) and anything that consumes its
tags — first of all the future quick-poses web app. **The JSON export is the only stable
interface.** XMP and file names are a convenience for other tools; the SQLite schema is
documented for debugging only and may change between versions.

## 1. JSON Lines export (contract)

```bash
flicktag export --json -o tags.jsonl              # every library
flicktag export --json --library "Pack 03"        # one library, to stdout
```

- UTF-8, one JSON object per line.
- **Line 1** is a header: `{"format_version": 1}`. Consumers must check it and refuse a
  higher major version they do not know.
- **Every following line** is one image whose status is `tagged` or `untagged`. Images in
  the trash, missing on disk or unreadable are left out.

```json
{"format_version": 1}
{"uid": "3fa9c2d1", "content_hash": "9b1d…e04a", "library": "Croquis-Pack-03",
 "path": "/home/me/Refs/Croquis-Pack-03/figure_standing_3fa9c2d1.jpg",
 "width": 2400, "height": 3600, "status": "tagged",
 "tags": {"subject": ["figure"], "pose": ["standing"], "view": ["profile"]}}
```

| Field | Type | Meaning |
|---|---|---|
| `uid` | string, 8+ hex chars | Permanent id of the image. Never changes, even when the file is renamed, moved or its metadata is rewritten. **Use this as the primary key.** |
| `content_hash` | string, 32 hex chars | BLAKE2b-128 of the file bytes as last seen by Flicktag. Changes when XMP is embedded. Useful to detect changed files, not as an id. |
| `library` | string | Name of the library (folder / image pack) the image belongs to. |
| `path` | string | Absolute path at export time. |
| `width`, `height` | int or null | Pixel size. |
| `status` | `"tagged"` or `"untagged"` | `tagged` = validated by the user at least once (it may still have zero tags). |
| `tags` | object | Category slug → list of tag slugs. Categories without tags are absent. Exclusive categories have at most one entry. |

Slugs are lowercase ASCII, accents stripped, words joined by `-` (e.g. `three-quarter`).
They are stable as long as the user does not rename the tag in Flicktag.

## 2. XMP metadata

Written after each validation, either embedded in the image (JPG, PNG, WebP) or in a
sidecar `<file>.<ext>.xmp` next to it (GIF, mode `sidecar`, or when embedding fails).
Mode is chosen in Settings: `embedded` (default), `sidecar`, `off`.

| Key | Content |
|---|---|
| `Xmp.dc.subject` | Flat list of tag *names*, e.g. `["figure", "standing"]` |
| `Xmp.lr.hierarchicalSubject` | `Category|tag` names, e.g. `["Pose|standing"]` (digiKam, Lightroom, darktable) |
| `Xmp.flicktag.uid` | The `uid` above; namespace `https://flicktag.local/ns/1.0/`, prefix `flicktag` |

Other metadata (EXIF, IPTC, other XMP keys) is preserved. An image validated with no tags
has no `dc:subject` / `hierarchicalSubject` but keeps its `flicktag:uid`.

## 3. File names (optional rename)

Rename is a manual, previewed, undoable batch (Esc → Rename from tags…). Default template:
categories with a *rename rank*, in rank order, then the uid — `{subject}_{pose}_{uid}`.

- `{<category-slug>}` → that category's tag slug; for a non-exclusive category, its first
  tag in display order. A category with no tag is dropped with the separator before it.
- `{uid}` is mandatory, so names are always unique.
- Stem is at most 80 characters (tag parts are shortened, never the uid); extension is
  kept, lowercased. Files stay in their folder; sidecars are renamed with them.

Example: `figure_standing_3fa9c2d1.jpg`, `hand_3fa9c2d1.png` (no pose), `3fa9c2d1.webp`.

Do not parse names to recover tags — use the export or the XMP.

## 4. SQLite (informational, not a contract)

Location: `$XDG_DATA_HOME/flicktag/flicktag.db` (default `~/.local/share/flicktag/`).

| Table | Purpose |
|---|---|
| `libraries` | One row per folder / pack (`root_path` unique) |
| `images` | `uid`, `content_hash`, `library_id`, `rel_path`, size, `status` (`untagged` / `tagged` / `trashed` / `unreadable` / `missing`), `xmp_pending`, cached `embedding` (float16) + `embed_model` |
| `categories` | `name`, `slug`, `exclusive`, `position`, `rename_rank` |
| `tags` | `category_id`, `name`, `slug`, `key` (shortcut; `a` = A, `A` = Shift+A), `prompt` (zero-shot text), `position` |
| `image_tags` | Assignments; `source` = `manual` or `suggested` |
| `feedback` | Per image and tag: 1 = suggestion kept or tag added, 0 = suggestion rejected |
| `journal` | Every validation, trash, rename (with before/after JSON) and XMP error |
| `settings` | Key → JSON value |
