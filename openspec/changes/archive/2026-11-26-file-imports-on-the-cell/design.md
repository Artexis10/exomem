# Design: file imports on the cell

## Context

A Cloud import stops the cell. cellctl's hold sets replicas to 0 while the import Job stages the files. Cleanup runs `cell-import discard --keep-adoption`, and release sets replicas back to 1 (`infra/cellctl/src/cellctl/imports.py`). Every Cloud import therefore ends with the cell starting. A local import (`exomem import`) stages into the same `_Imports/<id>` shape with the cell running.

`adoption_studio` action `file` already files a whole stage when its caller repeats the call with each `continuation`. It carries on from the saved phase and never starts a second run (spec `adoption-studio`, "One call files a folder"). The only missing piece is a caller that is always present.

The load target is 10,000 tenants. Each tenant runs its own cell, so the filer is per cell. The shared parts are Substrate's poll of the filing record and the media workers.

## Decisions

### A resident filer in the serving runtime

`import_filing.py` runs one worker thread in the HTTP serving runtime, started beside write intake (`server_runtime.py`, `_start_write_intake`). The stdio runtime does not start it: a local agent session drives `file` itself, and two serving processes never share one vault.

Rejected alternatives:
- **Filing in the import Job.** The cell is stopped then, under the import deadline and with no media workers. A large import would keep the vault offline for its whole filing time.
- **Substrate calling `file` on completion.** Home's owner channel times out after 5 s, while a `file` call takes tens of seconds. Substrate also has no durable per-import worker.
- **Existing workers.** Upkeep serves dreamer proposals and scans nothing. The media worker computes one file at a time. `maintenance_jobs` marks a dead owner's row `interrupted` instead of resuming it.

### Registration and the work list

The filer keeps one private-state record per stage at `<state>/import-filing/<import_id>.json`, written with the same private-JSON helper that import receipts use. A stage enters the list in two ways:
1. **At start**, the filer scans the import receipts and local stage records. A stage enters when its series file says `"filing": "cell"`, it has members, its record is not `done`, and it has no finished residual.
2. **On any `file` call** that names a stage, the call registers it. That gives agents one operation: after the first call, the cell finishes the stage on its own.
3. **On a `file` call with `wait: false`**, the call registers the stage under the caller's login and makes no filing call. See "Wake and register".

### One call at a time, exactly once

Before each call, the filer stores the mutation identity `import-file:<import_id>:<seq>` and the continuation in the record. After a crash, it repeats that exact call under the same identity, which is what `file`'s reconciliation and `writer_lease` need. The vault's mutation boundary makes the filer and a foreground `file` call take turns. Each import step (`plan` per item, `apply` per chunk, `finish` per staged copy, and the `file` chain between steps) ends at its next item boundary for a waiting writer, and stops starting items once the room left to its hold, less the delivery reserve, is spent, so the hold ends before the mutation lock's overdue limit unless one piece of work that no step can split is longer than that reserve (`yield-boundary-to-interactive-writes` names them). An item loop works for a turn of one second before a waiting writer ends its call, so two callers that file one stage each move its page on at every call. Between two calls the filer waits while a writer waits. A short in-process lock guards only the reads and writes of one record, never a call.

The filer runs `file` through `writer_lease.invoke_command`, under the principal that registered the stage. For a series-file registration that is the vault owner on the internal `cloud-import` surface.

### Stops

| Stop | Filer behaviour |
|---|---|
| `filing: done` | Record `done`; leave the list. |
| Free space | Park with a `free_space` need that carries the bytes the run records. Wake on a `file` call, a restart or an hourly space check. |
| `IMPORT_RECOVERY_UNRESOLVED` | Park with a `person` need. Only an operator or agent action resumes it. |
| `MUTATION_BUSY`, acknowledgement pending | Back off with a bounded exponential delay, at most 5 tries per hour, then park until the next start or `file` call. |
| `KB_NOT_INITIALIZED` | The vault has no Knowledge Base yet, and no one is present to create it. Make one `start` call with `initialize_kb`, under its own identity, then carry on with `file`. |
| The login that registered the stage ended | Park with code `IMPORT_FILING_LOGIN_ENDED` and a `login` need. A `file` call with `wait: false` from a valid login resumes it. |
| `CLOUD_CELL_READ_ONLY` | The cell serves read-only for a while, and the refusal comes before any work. Park with a `read_only` need and keep the stored call. Check again every minute; a `file` call or a restart checks sooner. |
| Any other error | Record the code and reason; park with a `support` need. |
| A discard of the stage | On the next start the residual exists; record `discarded`; leave the list. |

### The filing record answers status

After each call the filer writes the stage record. A status read with `path` set to the stage and `view="filing"` returns it. The read is O(1) and walks no runs or outcomes. The existing status views keep their behaviour. The reply is:

```json
{
  "state": "working | parked | done | discarded | unknown",
  "as_of": "2026-11-25T10:00:00Z",
  "progress": {"total": 120, "filed": 100, "skipped_duplicates": 2, "failed": 0, "left_out": 0, "remaining": 18},
  "left_out": {"by_reason": {"outside_run": 3}, "items": [{"path": ".draft.md", "code": "outside_run"}], "truncated": false},
  "stop": {"code": "INSUFFICIENT_SPACE", "reason": "..."},
  "needs": [{"need": "free_space", "bytes": 1048576}],
  "person": [],
  "rate": {"files_per_minute": 12.0, "over_calls": 4}
}
```

- `state`: `working`, `parked`, `done` or `discarded`. A stage with no record answers `unknown`, with `progress: null`, `stop: null`, no `as_of`, no `left_out` and no `rate`.
- `progress` is `null` until the first filer call has finished. `filed`, `skipped_duplicates` and `failed` are the counts of the last reply.
- `total` is the number of members that the stage's receipt or record lists, but the series file. The filer reads it once, when it registers the stage. It is `null` when the stage lists none, and `remaining` is then `null`.
- `left_out` in `progress` is 0 until `done`, then the number of members that adoption left out. `remaining` is `total - filed - skipped_duplicates - failed` before `done`. On `done` it is `total - filed - skipped_duplicates - left_out`, because each failed member is also left out. It is never below 0.
- The `left_out` object is present only on `done`. When the filer sees `done`, it reads the members that adoption did not file (`adoption_run.unfiled_members`) once and stores them in the record, so the view stays O(1). `by_reason` counts every member. `items` lists up to 100, sorted by path. `truncated` is true when more exist.
- Codes in `left_out` and in `stop.code` are the cell's closed sets (`adoption_consume.STAYS` and the stop table). Home words each code it knows; an unknown code shows a generic "Not filed" with the code.
- `needs` is a list of `{need}` objects, empty unless the stage is `parked`. `free_space` carries `bytes`, the next chunk plus the reserve that the volume must hold, and `null` when the stop does not know them. `login` says the registering login ended. `read_only` says the cell serves read-only, and the filer checks again by itself. `support` is any other stop.
- `person` is the cell's own text, unchanged, for agents. Home does not show it.
- `rate` is files filed or skipped per minute over the newest ten samples. The filer takes one sample when it registers a stage that nothing has filed (the start scan and a wake call), and one after each call. A stage that a foreground `file` call registered has no baseline, because the cell cannot know what that call filed. `rate` is absent until two samples exist and the window covers filed work, so a stage never shows 0.0 for work it did. `over_calls` is the number of samples in the window.

Honest values: a stage with no record answers `state: unknown`, never zero progress. A time left is never stated by the cell; a caller may derive one from `rate`, and must label it "at the measured rate".

### Wake and register

`adoption_studio` `file` with `wait: false` and the stage `path` registers the stage with the filer under the caller's principal, which replaces the principal of an ended login. It makes no `file` call in the request. It returns the filing view, as the status read does. The filer's thread makes the first call after the reply, so the request costs one receipt read and one record write. Home calls it when `needs` contains `login`, and once after an upload when the view says `unknown`.

The call refuses with `IMPORT_REQUEST_INVALID` on a server with no filer (the stdio runtime), on a path that is not a staged folder and on a stage that does not exist. It refuses with `IMPORT_FILING_LOGIN_NOT_REPLAYABLE` when the caller is a delegated authorization session: the owner may narrow such a session after the call, so the cell never replays it. A stage that is `done` or `discarded` answers as it is. The call changes only the cell's private filing record, so the writer classifier treats it like `transfer_artifact` cancel: it takes no vault write boundary and never waits behind a running filing call, but a read-only cell still refuses it.

A stored owner delegation would let a delegated session register a stage too. It would need a durable, revocable grant that names the stage, is signed by the owner and is checked before each filer call. That is a new authority, so this change does not build it.

### The origin envelope

The reserved series file `.exomem-series.json` already travels inside every Cloud archive. Inventory skips it and the residual excludes it. This change adds two optional keys:

```json
{"filing": "cell", "origin": {"label": "Photos from my phone", "device_family": "phone", "browser_family": "safari"}}
```

The reader stays lenient, so an older cell ignores the new keys. The label is the person's content, so it travels inside the encrypted archive and never in the C5 request.

Each archive member's `mtime` is the file's modification time. The Cloud unpacker already applies it. Adoption reads it from the inventory row.

Filing writes these frontmatter fields on each Source of a stage that has an `origin`:

| Field | Value |
|---|---|
| `origin_label` | The person's label, when given |
| `upload_device` | The device family, when given |
| `upload_browser` | The browser family, when given |
| `file_modified` | The member's mtime as an ISO time |

`file_modified` is written only when the stage has an `origin`. An older stage, whose mtimes were never set by its sender, therefore shows the file time as unknown instead of a wrong date. The file time is never a capture date (`recover-media-capture-dates` rules mtime out as capture evidence).

The existing fields keep their meaning: `imported_from` already holds `_Imports/<id>/<path>`, which names the import, and `collection_path` holds the original relative path. No separate import id field is added.

### Field names are registry data

`page-fields.yaml` gains four entries with `role: provenance` and the attributes `source: envelope | member-time` and, for an envelope field, `envelope: <key>`. `adopt._provenance_lines` loops over the provenance entries and branches only on `source`, never on an entry key. As with every role in this pack, a vault overlay can only retire an entry. An owner who does not want the upload device recorded retires `upload-device`.

The device and browser families are coarse closed sets, such as phone, tablet, desktop and safari, chrome, firefox, edge, other. The sender derives them from the user-agent client hints. They describe the uploading device, not where a file was made.

### What filing does with no assistant

`file` selects the whole stage with no classification rules, so every file becomes an unclassified Source. Markdown stays Source text and is not drafted into notes. An assistant may organise the Sources afterwards. Hidden files, zero-byte files, sync-conflict names, non-UTF-8 names and files that fail twice are listed in the residual with their reasons, as today.

## Risks

- **A step-path run on the same stage.** An assistant driving Adoption Studio step by step can race the filer. The per-stage lock serialises the calls. `file` already carries on an unfinished step-path run instead of starting another.
- **Upgrade windows.** A rolling upgrade treats a filing cell as idle and interrupts it. Filing resumes after the roll from the saved phase.
- **Slow filing on large imports.** Throughput belongs to `scale-agent-led-imports`. The filer only runs longer, and the record shows honest progress and the measured rate.
