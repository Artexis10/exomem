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

### One call at a time, exactly once

Before each call, the filer stores the mutation identity `import-file:<import_id>:<seq>` and the continuation in the record. After a crash, it repeats that exact call under the same identity, which is what `file`'s reconciliation and `writer_lease` need. A per-stage lock makes the filer and a foreground `file` call take turns; the loser of the lock waits for the next free slot instead of failing.

The filer runs `file` through `writer_lease.invoke_command`, under the principal that registered the stage. For a series-file registration that is the vault owner on the internal `cloud-import` surface.

### Stops

| Stop | Filer behaviour |
|---|---|
| `filing: done` | Record `done`; leave the list. |
| Free space | Park. Wake on a `file` call, a restart or an hourly space check. |
| `IMPORT_RECOVERY_UNRESOLVED` | Park with a `person` need. Only an operator or agent action resumes it. |
| `MUTATION_BUSY`, acknowledgement pending | Back off with a bounded exponential delay, at most 5 tries per hour, then park until the next start or `file` call. |
| `KB_NOT_INITIALIZED` | The vault has no Knowledge Base yet, and no one is present to create it. Make one `start` call with `initialize_kb`, under its own identity, then carry on with `file`. |
| Any other error | Record the code and reason; park. |
| A discard of the stage | On the next start the residual exists; record `discarded`; leave the list. |

### The filing record answers status

After each call the filer writes `{state, progress, stop, person, as_of, rate}` into the stage record. `state` is `working`, `parked`, `done` or `discarded`. `rate` is files filed per minute over the last ten calls, and it is absent until two calls have finished. A status read with `path` set to the stage and `view="filing"` returns that record. It is O(1) and walks no runs or outcomes. The existing status views keep their behaviour.

Honest values: a stage with no record answers `state: unknown`, never zero progress. A time left is never stated by the cell; a caller may derive one from `rate`, and must label it "at the measured rate".

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
