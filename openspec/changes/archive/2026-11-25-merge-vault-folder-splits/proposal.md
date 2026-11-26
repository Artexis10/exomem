## Why

A folder whose name differs from a sibling only by letter case or Unicode form (a "split", such as `Food/` and `food/`) makes a whole-vault export fail with `CASE_COLLISION`. The 2026-10-27 change stopped writers from starting a new split, but an existing split still grows: a write that names one spelling exactly lands under it, so both sides keep gaining files. The vault also offers no governed way to merge a split. A file-by-file hand merge is impractical for hundreds of files, leaves the emptied folders behind, and cannot remove an empty folder inside an append-only tree. And the `path_variants` audit is opt-in, so an owner does not see a split until an export fails.

## What Changes

- A write whose destination passes through a folder that has a variant sibling lands under one canonical spelling of that folder, whichever spelling the caller named. The canonical spelling is the spelling in NFC form, then one the vault's layout or project registry names, then the spelling whose folder holds the most entries, then the first in code-point order. A file that already exists at the requested spelling keeps it. The write still warns about the split.
- A new maintenance job kind `path-variants` previews every split: its canonical spelling, every move into it, the files it cannot move, and the other spellings it removes once empty. Its apply, bound to the preview's `plan_id`, moves every file through the ordinary file move, which rewrites links. The merge is one-way: bytes are kept, links are rewritten, and each moved page's lineage names its old path. It then removes each listed spelling left empty, also inside an append-only tree, and records the removal in the log. A remote preview and apply each run as a maintenance job.
- `path_variants` joins the default audit, and its finding names the merge.

## Capabilities

### Modified Capabilities

- `hosted-vault-portability`: writes converge on one spelling of a split folder; a governed merge folds a split; the audit runs `path_variants` by default.

## Impact

- Code: `vault.py` (destination re-spelling and the canonical choice), `layout.py` (declared folders), a new `path_variants.py` (preview, moves, empty-folder removal), `commands.py`, `operation_entries.py`, `maintenance_jobs.py` and `writer_lease.py` (the new job kind), `audit.py` (default category and finding text), `note.py` (a restricted writer's inbox folder takes existing spellings and is created through the held folder creator).
- The export result keeps carrying no path: the owner reads the split from the audit and the preview in their own memory.
- No migration. Nothing is merged unless the owner applies a sealed plan.
