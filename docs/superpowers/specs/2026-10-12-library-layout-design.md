# Library Layout — Design Spec

- **Date**: 2026-10-12
- **Status**: Draft, awaiting review
- **Amends**: `2026-10-11-flicktag-design.md` — lifts the v1 non-goal "moving files across
  folders during rename". The tag-based rename becomes a special case of the layout.

## 1. Purpose

The user dumps references into `~/Documents/Art/Références/**`, whose current folder tree
(`nu/assis/jpg`, `hands/webp`, purchased packs…) does not suit them. They want to tag
everything, nested folders included, and have the files laid out on disk from their tags,
with a single folder gathering everything still to tag.

### Success criteria

- A library can be given a **layout**: a path template driven by tags, plus an **untagged
  folder**.
- Once a layout is set, every scan of that library puts each misplaced file where the layout
  says, without the user asking. A **Tidy library** action does the same on demand.
- Files can be moved from one library into another one, following the destination's layout.
- Every move is previewed (first setup, manual actions) or reported (automatic tidy), and
  every batch is undoable.
- No duplicate images when a parent folder of an existing library is added.

### Non-goals

- Choosing a folder by hand for a single image.
- Moves across filesystems.
- Inferring tags from folder names. Where a file sits is never treated as tag data.

## 2. Layout rules

A layout belongs to a library and has two settings:

| Setting | Default | Meaning |
|---|---|---|
| `layout` (template) | categories with a rename rank as folders, then the default rename stem: `{subject}/{pose}/{subject}_{pose}_{uid}` | Target path of a tagged image, relative to the library root |
| `untagged_dir` | `À trier` | Folder, relative to the root, that gathers untagged images |

A library whose `layout` is NULL has no layout: nothing is ever moved automatically.

### Tagged images

- The template is split on `/`. Each segment is rendered with the existing rename rules: a
  placeholder whose category has no tag on the image is dropped together with the separator
  in front of it; a non-exclusive category contributes its first tag in display order; long
  values are shortened to fit `MAX_STEM`.
- A folder segment that renders empty is skipped, so the path gets shallower (an image with
  no pose lands in `figure/`).
- The last segment is the file stem. It must contain `{uid}` so names stay unique; folder
  segments may not be empty in the template text itself (`a//b` is rejected).
- The extension is kept, lowercased (as today).
- A template without `/` renames in place, inside the folder the image already sits in.
  This is the current "Rename from tags" behaviour, kept for compatibility. The untagged
  rule below still applies: a layout always gathers untagged images, slash or not.

### Untagged images

- Target: `<untagged_dir>/<original file name>`, wherever the file currently is — including
  a folder that looks right for it. An image whose tags are all removed goes back there.
- If that name is already taken by another file or another image of the batch, the uid is
  appended: `IMG_2231_217514e6.jpg`.

### Other statuses

`unreadable`, `missing` and `trashed` images are never moved.

## 3. Actions

1. **Organize into…** (File menu) — moves the images of the current library into a
   destination library (default: itself), following the destination's layout. Opens the
   Organize dialog. Apply saves the template and untagged folder as the **destination's**
   layout, so setting a layout for the first time always goes through this dialog's preview.
2. **Tidy library** (File menu) — applies the current library's layout to the current
   library only. Opens the same dialog, destination fixed to the current library, preview
   already computed; only misplaced files are listed. If the library has no layout yet, the
   dialog starts with the defaults and nothing is applied until the user clicks Apply.
3. **Automatic tidy** — after every scan of a library that has a layout (GUI open, Rescan,
   and the `flicktag scan` CLI), misplaced files are moved without a preview, as one
   journaled batch. The GUI shows a toast ("Tidy: 12 moved, 1 conflict"); the CLI prints the
   same counts. The batch is listed in the Organize dialog and can be undone there.

Images that change library keep their database row: tags, feedback, embedding and uid
follow them; only `library_id` and `rel_path` change.

## 4. Moving files safely

The existing two-phase rename in `core/renamer.py` is generalized:

- Phase 1 moves each source (and its `.xmp` sidecar) to a temporary name next to itself;
  phase 2 creates the target's parent folders and moves the temporary file to the target.
  Swaps and chains keep working.
- On any `OSError`, every file already moved is put back and every folder created by the
  batch is removed if empty, then the error is reported. Nothing is half-applied.
- The DB update runs in one transaction after the files moved; if it fails, the files are
  moved back (as today).
- After a successful batch, folders left empty by it are removed, walking up from each old
  parent until the library root. The root and the untagged folder are always kept.

### Conflicts (shown in the preview, op skipped, the rest proceeds)

- source file missing;
- two images get the same target (tagged images only — untagged ones get the uid suffix);
- target exists and is not part of the batch;
- source and destination root are on different filesystems (`st_dev` differs).

## 5. Undo

- Journal entries of kind `rename` record `{"library_id", "rel_path"}` in `before` and
  `after`. Entries written before this change have no `library_id` and mean "same library".
- Undoing a batch moves files back with the same two-phase mechanism, recreating original
  folders and removing folders the undo empties. Files moved again since the batch, or whose
  original place is now taken by an unrelated file, are left alone (as today).

## 6. Nested libraries

Libraries never overlap:

- Adding a folder **inside** an existing library is refused with a message naming that
  library.
- Adding a folder that **contains** existing libraries absorbs them, in one transaction and
  before the first scan: their images get the new library id and their `rel_path` prefixed
  with the child's path relative to the new root; the child libraries are deleted; the
  `current_library` setting follows. No file moves, so no duplicates appear.

For the user today: adding `Références` absorbs the `Références/boots/webp` library. The
separate `Documents/Art/À trier` library can then be emptied into `Références` with
Organize into….

## 7. Components

| Unit | Responsibility |
|---|---|
| `core/store.py` | Migration 2: `ALTER TABLE libraries ADD COLUMN layout TEXT; ADD COLUMN untagged_dir TEXT`. `Library` gains both fields; `set_layout()`. |
| `core/layout.py` (new) | Pure path computation: parse a template into segments, render a tagged image's relative path, compute an untagged image's relative path with the uid fallback. No file access beyond existence checks for the fallback. |
| `core/renamer.py` | `preview(store, source_id, dest_id, template, untagged_dir)` → ops; `apply` and `undo_batch` generalized to target library, folder creation and pruning; `tidy(store, library_id)` = preview + apply of ok ops for the automatic tidy. `RenameOp` gains the destination library id. |
| `core/library.py` | `add_library` overlap rules (refuse / absorb). `scan` itself stays a pure scan. |
| `__main__.py` | `flicktag scan` runs `tidy` after the scan when the library has a layout. |
| `ui/dialogs.py` | `OrganizeDialog` replaces `RenameDialog`: destination combo, template, untagged folder, preview table (current path → new path, problem), Apply, previous batches with Undo. |
| `ui/window.py` | File menu: "Organize into…", "Tidy library". The scan job runs `tidy` afterwards when a layout exists; the session queue and image cache refresh after any batch. |

`docs/FORMAT.md` is unchanged: the export already carries the absolute `path`, which simply
reflects the new location, and consumers identify images by `uid`.

## 8. Testing

- `layout`: segment rendering, empty folder skipped, `{uid}` required in the stem, `a//b`
  rejected, untagged target with uid fallback on collision.
- Preview: Tidy lists only misplaced images; an untagged image in a "right-looking" folder
  is listed; unreadable/missing/trashed are not.
- Apply across libraries: DB row follows (library, path, tags kept), sidecar moved, folders
  created, emptied folders pruned, untagged folder and root kept.
- Undo: files and library restored, folders recreated; legacy journal entries without
  `library_id` still undo.
- Failure mid-batch (injected `OSError`): every file back in place, created folders gone.
- Cross-filesystem op reported as a conflict (mocked `st_dev`).
- `add_library`: absorbing a child library (paths prefixed, no duplicate after scan);
  refusing a folder inside a library.
- Automatic tidy after scan only when a layout is set.
- UI smoke: Organize dialog preview → Apply → queue refreshed.
