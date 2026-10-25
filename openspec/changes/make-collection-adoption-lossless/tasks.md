## 1. Measurement

- [x] 1.1 Add `benchmarks/collection_ingest/` (generator, driver, scorer, README) for an invented collection of about 1,000 notes with mixed media and an answer key the generator writes. Verify: the same seed gives byte-identical output, and five hand-checked items per metric agree with the scorer.
- [x] 1.2 Record the baseline on the unchanged product, for the personal and Cloud-like profiles. Verify: the metrics files report 17 of 100 attachments landed through supported text formats, zero links preserved and zero classified homes.

## 2. Plan (D2, D3, D4)

- [x] 2.1 Freeze destinations first, then resolve each text item's wikilinks, embeds and local Markdown links (existing wikilink helper extended in place; `markdown_regions` for CommonMark) by collection path, note-relative path, unique basename, then unique alias; record resolved and unresolved targets, tags, aliases and rejected tags; hash the complete planned actions into `plan_id`. Verify: a test with path, basename, alias, embed and shared-basename links fails on main.
- [x] 2.2 Accept `source_kind` and `domain` on `select` folder rules, registered results only, as part of the selection identity; target classified items at `source_directory`; compute index and log deltas from actual destinations. Verify: a test lands a ruled folder in `Sources/<Kind>/<Domain>/` and refuses an unregistered kind with no selection change.
- [x] 2.3 Record `import_collection` and `collection_path`; check earlier imports by them (falling back to `imported_from`); record `existing_ref`, or `previous_import` for a changed original. Verify: a re-staged unchanged collection under a new path plans every item with `existing_ref`.

## 3. Apply and finish (D1, D5, D6)

- [x] 3.1 Factor the artifact commit out of `add.add` into one streaming seam that takes an exact destination, import provenance and the ceiling; make non-text and non-UTF-8 files import form `artifact`; request extraction after each artifact commit. Verify: a test imports a PDF and an image whose artifact bytes equal the originals and finds their extraction requests; capture tests pass unchanged.
- [x] 3.2 Render the fence verbatim (no stripping) and the `## Links` section outside it; carry tags and aliases into front matter. Verify: the graph shows an edge from an imported note to an imported target; the fenced text with trailing whitespace hashes to `original_sha256`.
- [ ] 3.3 Bound `plan` and `apply` calls by the remaining request budget with a server-issued `continuation`; write text outcomes in their commit batch; recheck identity under the guard; fail an occupied frozen destination with `TARGET_OCCUPIED`; set `applied` only when every item has an outcome; report a budget stop as remaining work in `status`. Verify: a test with a tiny budget needs several calls, writes each item once, replays a repeated cursor without writing, and leaves originals byte-identical.
- [x] 3.4 Add `coverage` to `finish`, record hidden folders as counted inventory rows, and match recall hits on the exact target path. Verify: a test with a pending image, an unresolved link and a hidden folder counts all three honestly.

## 4. Guidance and surface (D7)

- [x] 4.1 Teach the collection route in the scaffold's `exomem-ingest` skill and name every media kind in the `adoption_studio` description; update the tool schema fixture. Verify: `tests/test_scaffold_no_leak.py` and the tool schema tests pass.

## 5. Proof and delivery

- [ ] 5.1 Run the benchmark after the change on the same seed and profiles from a clean, committed product tree; record before and after under `docs/benchmarks/`. Verify: every collection item is landed or refused with a code, and a re-run creates no Source.
- [ ] 5.2 Run the full suite, the privacy gate and `openspec validate --all --strict`; get an independent review; merge.
