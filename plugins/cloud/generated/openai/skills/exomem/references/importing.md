# Importing a folder or archive

Use this when the person wants existing files in the Knowledge Base: a folder, a ZIP or a notes export. Adoption never changes an original, and the person keeps their own copy in every route. Fields of each operation are in [operations](operations.md#collection-adoption).

## Cloud prerequisites

Call `observe_transfer(action="import_handoff")` before sending local files to Cloud. Read `open_import` and the returned routes.
Use the CLI route's `argv_folder` or `argv_zip`, replacing its path placeholder with the real absolute path.
The returned command includes the endpoints and can use `uvx` without a global Exomem installation.

Check that the command's runner and `age` are on `PATH` before starting it.
`uvx` comes with [uv](https://docs.astral.sh/uv/getting-started/installation/); [age](https://github.com/FiloSottile/age#installation) has separate installation instructions.
Use their official instructions for the current platform when either is missing. Cloud imports need no local vault or Exomem service.

An unversioned `uvx exomem` command can reuse a cached or installed CLI. Refreshing the connector or plugin does not update it.
After a CLI fix ships, use `uvx exomem@latest` in place of that prefix when retrying the returned command.
Keep its endpoints, other arguments and existing checkpoint. [uv's tool-version guidance](https://docs.astral.sh/uv/concepts/tools/#tool-versions) explains this refresh.

The CLI opens its own browser sign-in, even when the MCP connector is already authenticated.
Explain this before starting, and have the person authorize the same Cloud account.
Keep the background command's output available while authorization and filing continue.
If another import is open, inspect its progress through MCP and agree whether to resume it or cancel it before starting another.

## Flow
1. Send the files to the server. Never retype or paste file contents.
   - Local server with a shell: run `exomem import <folder>`. It copies the folder into `_Imports/<id>/` and prints the path to start on.
   - Exomem Cloud with a shell: start the returned CLI command in the background, never in the foreground. It can run for hours: a folder larger than the room the cell reports goes as several windows, each an ordinary import with its own ID. The folder or ZIP is encrypted before it leaves the machine.
     The command files each window itself once it lands, which makes room for the next one, and it never discards. Read its output for the window IDs and for a filing stop, which names the cell's code and reason; that window stays staged for a rerun or for step 3. `--no-file` only uploads: then file each landed window yourself (step 3). An older cell refuses `file`: the command says so and leaves the windows staged for you.
     The command waits for room, and it stops with the members not yet sent if the cell cannot take them. Until it finishes, those members are only at the source.
   - Exomem Cloud without a shell: give the person the returned CLI command or Home link. For a folder larger than the room, give only the command: Home sends one import. Never try to upload the files yourself.
2. Wait until the import, or the first window, is `done`. On Cloud, poll `observe_transfer`. `read_import` status lists each landed window's `import_id`, so later windows are found there.
3. File it: call `import_memory(operation={"kind": "file", "path": "_Imports/<id>"})`. Add `collection` when the person names one. While the reply says `filing: "working"`, call `file` again with the same `path` and the `continuation` from that reply. Stop when `filing` is `done` or `stopped`. The server plans, applies and finishes each page, and starts the next page itself. A page whose files fail retries them once, and filing goes on to the next page; files that still fail stay in `progress.failed` and the residual. A `file` call with only the `path` carries on the import of that folder, and answers `done` again once it is done. A `collection` other than the import's own is refused, and the error names the import's collection.
   - `person` lists what only the person can do: `free_space`, `review_residual`, `discard` and `report_to_operator`. Do nothing else for the person without asking.
   - `filing: "stopped"` with `free_space`: tell the person, and call `file` again with the `continuation` after space is free.
   - `filing: "stopped"` with another `stop.code`: report the code and the `progress` counts. Files already filed stay filed.
   - A call that stops because the server is old (it refuses `file` as an unknown kind) uses step 4.
4. The step path files a page with separate calls. Use it on an older server, for a folder with own-note rules (`adopt_as: "note"`), or to file hidden files with `include_hidden=true`: `file` applies none of these. Call `start` on `_Imports/<id>`, with `collection` when the person names one. Then `select` the stage folder, `plan`, `apply`, then `finish`. While a reply carries a `continuation`, call the same action again with it. While a reply carries `next_after`, call `start` again with only `after` set to it, then repeat this step. Pages file in order and share one collection.
5. Read the `residual` that the last page's `finish` reports (in `filed.residual` after `file`): the files still staged, by reason, with counts and bytes. Its `summary` says in plain words how many files need review. Review those files with the person. After `file`, report the whole import from the reply's `summary` and `progress` counts, then the residual. `filed.last_finished_page` is the last `finish` report, about that one page only.
6. Read `read_import(operation={"kind": "status"})`. It reports each window's unfiled members, the cell's budgets and the effective file limit; a figure that reads `unknown` is not zero. While the file limit reads `unknown`, do not say whether a file fits: say the limit is unknown. Tell the person to keep the source folder while `keep_source.keep` is true.
7. Discard the stage only after the person agrees. For a series of windows, ask once for the series.
   - Cloud: `transfer_memory(action="import_discard", import_id=<id>, confirm=true)` deletes the staged folder for good.
   - Local: no Exomem command discards a stage. The person deletes `_Imports/<id>` by hand.
   - A discard keeps a residual record of each member that adoption did not file or `finish` did not verify. Status lists those members as "discarded, not filed", with the reason as the `code`.
   - If a discard does not finish, `observe_transfer` shows the import stuck in `cleanup_pending`, and its `error_code` may name the refusal. Read `read_import` status for that window. `renames_pending` lists Knowledge Base paths: run `apply`, or move each file and keep the source. The cell retries the discard on its own, so another `import_discard` changes nothing. Keep the source until the window reads `discarded`.
   - A window in state `unreadable` has a record that cannot be read. No tool you hold discards it, and the person cannot either. Keep the source, tell the person, and leave the stage. Only the operator of Exomem Cloud can discard it, with `--walk-stage`.
8. Acknowledge residual paths only when the person confirms they no longer need to keep them: `import_memory(operation={"kind": "acknowledge", "import_id": <id>, "paths": [...]})`. This is the person's decision. Acknowledged paths stop counting toward `keep_source`. No tool undoes it; the operator can restore the entries by editing the record. A call without paths is refused.

## Stops
- `apply_stop` with `INSUFFICIENT_SPACE`: stop, even though the reply carries a `continuation`. The next `apply` stops at the same chunk until the volume has the chunk's bytes plus the reserve free. Tell the person, and call `apply` again with the `continuation` after space is free. A `file` call reports the same stop as `filing: "stopped"` with `free_space` in `person`.
- A file over the adoption ceiling stays staged and is listed in the residual with its code and the ceiling.
- A file that changed after `plan` is refused for that item, and its neighbours still file.

## Interrupted apply

`MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN` means data committed, but the call's exact terminal result could not be saved.
`MUTATION_OUTCOME_UNKNOWN` does not establish how many files committed.
Keep the original folder, staged import, run and original tool call until recovery resolves their state.
Refreshing the connector or plugin does not repair the Cloud server or update its runtime.

On servers through v0.161.1, per-run status includes the full inventory, plan and outcomes.
If that reply is too large, `read_import(operation={"kind":"status"})` lists compact run rows without a `run_id`.
That listing does not reconcile the interrupted call.

On v0.162.0 and later servers, the status schema includes `view`. Use this sequence:

1. Read `status` with the existing `run_id`; expect a compact summary with counts, `plan_id`, `revision` and interruption state.
2. Read details with `view="outcomes"`, `limit=100` and that `revision`; expect a bounded page with `next_offset`.
3. Carry `next_offset` as `offset` and keep the revision; expect each page from the same saved run.
4. If `IMPORT_STATUS_CHANGED` returns, read the summary again; expect a new revision before restarting the detail pages.

Other detail views are `inventory`, `selection`, `plan` and `stale`.
One oversized row appears in `omitted` with `ROW_TOO_LARGE`; it is uninspected, not an empty or successful item.
`view="full"` remains unbounded for the browser and explicit compatibility callers.

On a v0.162.0 or later server, repeat the interrupted `import_memory` apply call exactly once. Do the same for an interrupted `file` call. The server repeats that call's own `apply` exactly, or runs none when the call never reached one, and the reply says `filing: "working"` with a fresh `continuation`.
The `view` field in the status schema shows that the server has this repair; an older server cannot reconcile the call.
Keep its account, client, arguments, original `continuation` and explicit idempotency key when present.
The server reconciles the same abandoned mutation identity before it resumes the retained scope.
Do not substitute a continuation from a later status reply or change retry filters for this call.
After recovery returns a new continuation, follow that continuation normally.
If replay returns only a committed envelope, read the summary again for the current phase and continuation.

An active writer returns `MUTATION_ACKNOWLEDGEMENT_PENDING`; wait for that call before retrying its exact identity.
`IMPORT_RECOVERY_UNRESOLVED` means the server cannot prove the original scope or exact custody.
Preserve the run and staging, and report that code with the server version and run metadata to the operator.
Do not create another run, change the mutation identity, force an import or discard the stage to bypass it.
Some older runs lack enough evidence, especially failed-only retries or partially published chunks without a complete receipt.
Recovery confirms filing and custody; `finish` must still report recall coverage and any pending extraction or visibility.

## What adoption leaves staged
- Each file that is filed as an artifact loses its staged copy once its Knowledge Base copy matches (`consumed` in the reply). `consume=false` on `start` keeps every staged copy.
- A text file loses its staged copy at `finish`, once its Source page holds the exact text.
- A text file keeps its copy while its note is owed. `apply` is refused after `finish`, so the copy stays staged and the residual lists the file as `pending`.
- A text file also keeps its copy when `finish` finds a difference, and the residual lists it as unfiled. `source_differs` means the Source page no longer holds the exact text, so the staged copy is the only exact one. `copy_changed` means the Source page holds the text, but `finish` could not safely remove the staged copy: it changed after the plan, the stage manifest does not list it, or the removal failed. Review it with the person; do not discard it unreviewed.
- `finish` removes only the text of its own page. When its time budget runs low it keeps the rest and reports them as `unchecked`: not yet verified against their Source pages. Review them with the person; a finished run takes no second `finish`.
- A folder made by hand under `_Imports/` is never consumed.

Report the `coverage` from `finish`, including what was refused or is pending, and then the residual.
