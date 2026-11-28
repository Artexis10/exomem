## 1. Registry and series file

- [x] 1.1 Add the `provenance` role to the page-field registry loader. Add four entries to `vocabulary/packs/core/page-fields.yaml`: `origin-label`, `upload-device`, `upload-browser` (`source: envelope`) and `file-modified` (`source: member-time`). Refuse an overlay entry that grants the role. Verify: the registry tests load the entries, and an overlay that declares one is refused.
- [x] 1.2 Read the optional `filing` and `origin` keys of `.exomem-series.json` in `import_stage.py`, leniently. Verify: an older series file reads as before, and an unknown value is ignored.

## 2. Provenance on filed Sources

- [x] 2.1 Carry the stage's `origin` into the adoption run, and each inventory row's mtime into its plan item. Write the provenance fields in `adopt._provenance_lines` by looping over the provenance entries and branching only on `source`. Verify: a stage with an origin files Sources that carry the four fields. A retired entry is not written. A stage without an origin gets none.

## 3. The import filer

- [x] 3.1 Add `import_filing.py`, with a per-stage private-state record, a startup scan, registration from the series file and from `file` calls, a per-stage lock, identity-before-call and exact replay, and the stop table in `design.md`. Start it from the HTTP serving runtime only. Verify with tests on a temporary vault: a registered stage is filed with no client call. A crash between record and reply replays the same identity and files each file once. A foreground `file` call and the filer take turns. A free-space stop parks and resumes. A discarded stage ends `discarded`.
- [x] 3.2 Write the filing record after each call and serve `adoption_studio status` `view="filing"` from it at constant cost. Verify: the reply for a stage with no record is `state: unknown` with no counts. A read does not open run or outcome files.
- [x] 3.3 Update the `import_memory` and `observe_transfer` tool text and the skill scaffold reference: the cell finishes a registered import, and the filing view reports it.
- [x] 3.4 Make the filing view carry `progress.total` (staged members without the series file, null when unknown), `left_out` and `remaining` in `progress`, a structured `needs`, `left_out` on `done` (reason counts, up to 100 items, `truncated`), and a `rate` that appears only once two samples cover filed work. Store what `done` needs in the filing record, so the read stays one record read. Verify: tests for a done stage that left files out, a stage with no manifest, a stage filed in one call after its registration, and 100 or more left-out files.
- [x] 3.5 Let `adoption_studio` `file` with `wait: false` register the stage under the caller's principal and answer the filing view with no filing call in the request. Verify: a test registers a stage after its login ended, spies on `file`, and sees only the filer thread call it. A login the cell cannot replay is refused and registers nothing.
- [x] 3.6 Treat `CLOUD_CELL_READ_ONLY` as a wait: park with a `read_only` need, keep the stored call and check again every minute. Verify: a test refuses the call with read-only mode on, sees `needs` of `read_only` and a 60 s check, and then files the stage once the mode ends.
- [x] 3.7 Prove that the public doors reach the filing view and the wake, and name both calls in the `observe_transfer` text and the skill reference. Verify: a test drives the in-process MCP server with `inspect_memory` `imports.status` (`view: "filing"`) and `manage_memory` `imports.file` (`wait: false`), and the cell then files the stage. The test fails when the door drops `wait`.

## 4. CLI parity

- [x] 4.1 Make `exomem cloud import` write `filing` and `origin` (browser family `cli`, `--label`) into each window's series file unless `--no-file`, and set each member's mtime. Verify: the CLI tests read the series file and member times from a built window.
- [x] 4.2 Add `exomem cloud import --no-wait`: write `filing: cell` and the origin, and make no `file` call. It cannot go with `--no-file` or `--collection`. Verify: a CLI test reads the series file and sees no `file` call.

## 5. Proof

- [x] 5.1 Add a `home-import` scenario to the Cloud rehearsal. Build a fixture tree with loose files, a nested folder, the members of an expanded zip, a zero-byte file, a hidden file and a non-UTF-8 text file. Send the tree with `exomem cloud import --no-wait`, which writes `filing: cell` and the origin. Make no MCP `file` call. Restart the cell once during filing, and poll the filing view until `done`. Verify against the fixture manifest: each eligible file has one Source with its `collection_path`, `file_modified` and origin fields, junk and hidden files are listed with reasons, and the counts add up. The scenario is built and its fixture and checks are unit-tested; its first live run is the `home-import` mode of the `Cloud rehearsal` workflow.
- [x] 5.2 Run the full corpus at the batch boundary, run the independent review, merge and release to cells.
