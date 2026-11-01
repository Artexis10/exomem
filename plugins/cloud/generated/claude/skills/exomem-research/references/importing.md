# Importing a folder or archive

Use this when the person wants existing files in the Knowledge Base: a folder, a ZIP or a notes export. Adoption never changes an original, and the person keeps their own copy in every route. Fields of each operation are in [operations](operations.md#collection-adoption).

## Flow
1. Send the files to the server. Never retype or paste file contents.
   - Local server with a shell: run `exomem import <folder>`. It copies the folder into `_Imports/<id>/` and prints the path to start on.
   - Exomem Cloud with a shell: run `exomem cloud import <folder>`. The folder or ZIP is encrypted before it leaves the machine.
     A folder larger than the cell's media volume goes as several windows, each sized by the budget that the cell reports. The command waits for room, and it stops with the members not yet sent if the cell cannot take them. Until it finishes, those members are only at the source.
   - Exomem Cloud without a shell: give the person the exact `exomem cloud import <folder>` command to run themselves, or the Home link from `observe_transfer(action="import_handoff")` for a browser upload. Never try to upload the files yourself.
2. Wait until the import ends. On Cloud, poll `observe_transfer` until the import is `done`.
3. Call `import_memory(operation={"kind": "start", "path": "_Imports/<id>"})`. Add `collection` when the person names one.
4. File the page: `select` the stage folder, `plan`, `apply`, then `finish`. While a reply carries a `continuation`, call the same action again with it.
5. While a `start` or later reply carries `next_after`, call `start` again with only `after` set to it, then repeat step 4. Pages file in order and share one collection.
6. Review the residual with the person. The last page's `finish` reports `residual`: the files still staged, by reason, with counts and bytes.
7. Read `read_import(operation={"kind": "status"})`. It reports each window's unfiled members, the cell's budgets and the effective file limit; a figure that reads `unknown` is not zero. Tell the person to keep the source folder while `keep_source` is true.
8. Discard the stage only after the person agrees.
   - Cloud: `transfer_memory(action="import_discard", import_id=<id>, confirm=true)` deletes the staged folder for good.
   - Local: no Exomem command discards a stage. The person deletes `_Imports/<id>` by hand.
   - A discard keeps a residual record of each member that adoption did not file. Status lists those members as "discarded, not filed".
   - `IMPORT_RENAMES_PENDING`: run `apply`, or move the file and keep the source.
   - `IMPORT_RECEIPT_UNREADABLE`: the operator of the cell discards again with `--walk-stage`.
9. Acknowledge residual paths only when the person confirms they no longer need to keep them: `import_memory(operation={"kind": "acknowledge", "import_id": <id>, "paths": [...]})`. This is the person's decision. The paths stay listed as `acknowledged`, and the operator can undo it by hand. A call without paths is refused.

## Stops
- `apply_stop` with `INSUFFICIENT_SPACE`: stop, even though the reply carries a `continuation`. The next `apply` stops at the same chunk until the volume has the chunk's bytes plus the reserve free. Tell the person, and call `apply` again with the `continuation` after space is free.
- A file over the adoption ceiling stays staged and is listed in the residual with its code and the ceiling.
- A file that changed after `plan` is refused for that item, and its neighbours still file.

## What adoption leaves staged
- Each file that is filed as an artifact loses its staged copy once its Knowledge Base copy matches (`consumed` in the reply). `consume=false` on `start` keeps every staged copy.
- A text file stays staged until the discard. Adoption does not consume it.
- A folder made by hand under `_Imports/` is never consumed.

Report the `coverage` from `finish`, including what was refused or is pending, and then the residual.
