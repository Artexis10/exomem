# Importing a folder or archive

Use this when the person wants existing files in the Knowledge Base: a folder, a ZIP or a notes
export. Adoption never changes an original, and the person keeps their own copy in every route. A
file whose bytes the vault already holds is not stored again, and a re-run writes nothing.
`read_instructions(topic="imports.file")` lists the fields of each import operation. To copy a few
chosen files, use the import rows in [tools](tools.md#intent-to-call) instead.

## Cloud prerequisites

Call `observe_transfer(action="import_handoff")` before you send local files to Evermatter Cloud.
Read `open_import` and the returned routes. Use the CLI route's `argv_folder` or `argv_zip`, with
the real absolute path in place of its placeholder. The command carries the endpoints and runs
through `uvx`, so no global install is needed. Cloud imports need no local vault or service.

Check that the command's runner and `age` are on `PATH` before you start it. `uvx` comes with
[uv](https://docs.astral.sh/uv/getting-started/installation/), and
[age](https://github.com/FiloSottile/age#installation) has its own instructions. Use the official
instructions for the current platform when either is missing.

An unversioned `uvx` command can reuse a cached CLI, and a connector refresh does not update it.
After a CLI fix ships, retry the returned command with its prefix pinned to the fixed release. This
package's release is `uvx evermatter@<exomem-release>`. Keep the endpoints, the other arguments and
the existing checkpoint. [uv's tool-version guidance](https://docs.astral.sh/uv/concepts/tools/#tool-versions)
explains pinned versions.

The CLI opens its own browser sign-in, even when the connector is already signed in. Explain this
before you start, and have the person authorize the same Cloud account. Keep the background
command's output available while sign-in and filing continue. If another import is open, read its
progress first and agree with the person whether to resume or cancel it.

## Flow

1. Send the files to the server. Never retype or paste file contents. Pick the route by server and
   client:
   - Personal server, client with a shell or a networked sandbox: send the folder as one archive.
     `read_instructions(topic="upload_files")` gives the three steps.
   - Personal server, shell on its machine: run `exomem import <folder>` (CLI only). It copies the
     folder into a stage and prints its path.
   - Personal server, client with neither a shell nor a sandbox: give the person
     `exomem import <folder>`.
   - Evermatter Cloud with a shell: start the returned CLI command in the background, never in the
     foreground. It can run for hours. A folder larger than the cell's room goes as several windows,
     each an ordinary import with its own ID. The folder is encrypted before it leaves the machine.
     The command files each window once it lands, and never discards. It also asks the cell to
     finish filing each window, so a stop or an exit of the command leaves no landed window unfiled.
     `--label` names where the files came from, and the Sources record it. Read its output for window
     IDs and filing stops. With `--no-file`, it only uploads, and you file each window in step 3.
     With `--no-wait`, it uploads and asks the cell to file, and makes no `file` call itself.
     Until the command finishes, the members it has not sent exist only at the source.
   - Evermatter Cloud without a shell: give the person the returned CLI command or the Home link. For
     a folder larger than the room, give only the command. Never upload the files yourself.
2. Wait until the import, or its first window, reads `done`. On Cloud, poll `observe_transfer`.
   `inspect_memory(operation="imports.status")` lists each landed window's ID.
3. File it. A cell that was asked to file, or that has seen any `imports.file` call on the stage,
   finishes it by itself. Read `inspect_memory(operation="imports.status", arguments={"path": "<stage path>",
   "view": "filing"})` until `state` is `done`.
   - `state` is `working`, `parked` (with the `stop` code and `needs`), `done` or `discarded`.
     `unknown` means the cell has no record of the stage, never zero files. A parked stage resumes
     on a later `imports.file` call or a restart. A `done` view lists in `left_out` the members
     that were not filed, by reason; tell the person.
   - `manage_memory(operation="imports.file", arguments={"path": "<stage path>", "wait": false})`
     registers the stage under your login, which resumes one whose login ended, and files nothing
     in the call.

   To file at once, call `manage_memory(operation="imports.file", arguments={"path": "<stage path>"})`, or
   with an uploaded archive `arguments={"upload": "<token>"}`. Never pass both. Add `collection` when
   the person names one. While the reply says `filing: "working"`, call again with the same `path`
   and the reply's `continuation`. Stop at `done` or `stopped`. The server plans, applies and
   finishes each page itself, and retries a failed file once.
   - A repeat with the same `upload` or `path` carries on the same import and unpacks nothing again.
   - When filing stops for space, tell the person. Call again with the `continuation` once space is
     free. An archive stays held unless the reply says it was not kept.
   - When filing stops for another reason, report the reason and the progress counts. Files already
     filed stay filed. A refused archive must be fixed, packed and uploaded again.
   - The reply's `person` list names what only the person can do. Do none of it without asking.
4. Use the step path when the folder has own-note rules or the person wants hidden files filed:
   `imports.file` applies neither. Call `imports.start` on the stage, then `imports.select`,
   `imports.plan`, `imports.apply` and `imports.finish`. While a reply carries a `continuation`, call
   the same action again with it. While a reply carries `next_after`, call `imports.start` again
   with `after` set to it, then repeat this step.
   - A folder of the person's own notes can become notes. Ask which folders hold them; never guess
     from file names. Give that folder the rule `{"path": ..., "adopt_as": "note", "note_type": ...}`,
     with a `note_type` that `inspect_memory(operation="schema.inspect", arguments={"subject": "note-types"})`
     lists as compiled, and `project` or `domain` where that type needs one. Each file keeps its
     Source and gets a draft note that cites it. A draft is not live; promote it after review.
5. Report the whole import from the reply's summary and progress counts, then the residual: the
   files still staged, by reason. The last page's own result describes that page only, never the import. Review those files with the person.
6. Read `inspect_memory(operation="imports.status")`. A figure that reads `unknown` is not zero:
   while the file limit is unknown, do not say whether a file fits. Tell the person to keep the
   source folder while status says to keep it.
7. Discard the stage only after the person agrees, once for a series of windows.
   - Cloud: `transfer_memory(action="import_discard", import_id="<id>", confirm=true)` deletes the
     stage for good.
   - Personal server: no Evermatter command discards a stage. The person deletes it by hand.
   - A discard keeps a record of each member that was not filed or verified.
   - A Cloud discard that does not finish shows as pending cleanup. Read the window's status:
     apply pending renames or move each file, and keep the source. The cell retries the discard
     itself, so another discard changes nothing. Keep the source until the window reads `discarded`.
   - When a window's record cannot be read, no tool discards it. Keep the source, tell the person and
     leave the stage; only the Cloud operator can remove it.
8. Acknowledge residual paths only when the person confirms they no longer need them:
   `manage_memory(operation="imports.acknowledge", arguments={"import_id": "<id>", "paths": ["<path>"]})`.
   Acknowledged paths stop counting toward keeping the source. No tool undoes it.

## Stops

- A stop for space holds even when the reply carries a `continuation`. The next apply stops at the
  same chunk until the volume has room. Tell the person, and continue once space is free.
- A file over the adoption ceiling stays staged and appears in the residual.
- A file that changed after the plan is refused, and its neighbours still file.

## Interrupted apply

When an apply or `imports.file` call is interrupted, the data may have committed without its reply.
Keep the original folder, the stage, the run and the original call until recovery settles their
state. A connector refresh does not repair the server.

1. Read `inspect_memory(operation="imports.status", arguments={"run_id": "<run>"})`. Expect a compact
   summary with counts, the plan, a `revision` and the interruption state.
2. For details, read `view="outcomes"` with `limit` and that `revision`, carrying `next_offset` as
   `offset`. Other views are `inventory`, `selection`, `plan` and `stale`. An omitted oversized row
   is uninspected, not an empty or successful item. `view="filing"` is not a run view: it takes the
   stage `path` and no `run_id` (step 3 of the flow).
3. If the status changed while you paged, read the summary again and restart the detail pages.
4. Repeat the interrupted call exactly once, with its account, client, arguments, original
   `continuation` and idempotency key. Expect `filing: "working"` with a fresh `continuation`.
   Never substitute a continuation from a status reply, and never change retry filters.
5. Follow the new continuation as normal. If the replay returns only a committed reply, read the
   summary again for the current phase.

While another writer holds the call, wait for it, then retry the exact identity. When the server
cannot prove the original scope or custody, keep the run and stage, and report the code, the server
version and the run to the operator. Never start another run, change the identity, force the import
or discard the stage to get past it. Recovery confirms filing and custody; `finish` still reports
recall coverage and pending extraction.

## What adoption leaves staged

- A file filed as an artifact loses its staged copy once its Knowledge Base copy matches.
  `consume=false` on `imports.start` keeps every staged copy.
- A text file loses its staged copy at `finish`, once its Source page holds the exact text. It keeps
  the copy while its note is owed, and the residual lists it as pending.
- A text file also keeps its copy when `finish` finds a difference. Then the staged copy may be the
  only exact one, or it changed after the plan. Review it with the person; never discard it
  unreviewed.
- When `finish` runs short of time, it reports the rest as unchecked. Review those with the person;
  a finished run takes no second `finish`.
- A folder made by hand in the stage area is never consumed.

Report the coverage from `finish`, including what was refused or is pending, and then the residual.
