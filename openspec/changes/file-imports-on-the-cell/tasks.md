## 1. Registry and series file

- [ ] 1.1 Add the `provenance` role to the page-field registry loader. Add four entries to `vocabulary/packs/core/page-fields.yaml`: `origin-label`, `upload-device`, `upload-browser` (`source: envelope`) and `file-modified` (`source: member-time`). Refuse an overlay entry that grants the role. Verify: the registry tests load the entries, and an overlay that declares one is refused.
- [ ] 1.2 Read the optional `filing` and `origin` keys of `.exomem-series.json` in `import_stage.py`, leniently. Verify: an older series file reads as before, and an unknown value is ignored.

## 2. Provenance on filed Sources

- [ ] 2.1 Carry the stage's `origin` into the adoption run, and each inventory row's mtime into its plan item. Write the provenance fields in `adopt._provenance_lines` by looping over the provenance entries and branching only on `source`. Verify: a stage with an origin files Sources that carry the four fields. A retired entry is not written. A stage without an origin gets none.

## 3. The import filer

- [ ] 3.1 Add `import_filing.py`, with a per-stage private-state record, a startup scan, registration from the series file and from `file` calls, a per-stage lock, identity-before-call and exact replay, and the stop table in `design.md`. Start it from the HTTP serving runtime only. Verify with tests on a temporary vault: a registered stage is filed with no client call. A crash between record and reply replays the same identity and files each file once. A foreground `file` call and the filer take turns. A free-space stop parks and resumes. A discarded stage ends `discarded`.
- [ ] 3.2 Write the filing record after each call and serve `adoption_studio status` `view="filing"` from it at constant cost. Verify: the reply for a stage with no record is `state: unknown` with no counts. A read does not open run or outcome files.
- [ ] 3.3 Update the `import_memory` and `observe_transfer` tool text and the skill scaffold reference: the cell finishes a registered import, and the filing view reports it.

## 4. CLI parity

- [ ] 4.1 Make `exomem cloud import` write `filing` and `origin` (browser family `cli`, `--label`) into each window's series file unless `--no-file`, and set each member's mtime. Verify: the CLI tests read the series file and member times from a built window.

## 5. Proof

- [ ] 5.1 Add a `home-import` scenario to the Cloud rehearsal. Build a fixture tree with loose files, a nested folder, the members of an expanded zip, a zero-byte file, a hidden file and a non-UTF-8 text file. Write a series file with `filing: cell` and an origin, and send the tree through the real import Job. Make no MCP `file` call. Restart the cell once during filing, and poll the filing view until `done`. Verify against the fixture manifest: each eligible file has one Source with its `collection_path`, `file_modified` and origin fields, junk and hidden files are listed with reasons, and the counts add up.
- [ ] 5.2 Run the full corpus at the batch boundary, run the independent review, merge and release to cells.
