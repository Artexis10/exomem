## 1. Design gate

- [x] 1.1 Run an independent critique of the design and settle it. Verify: the critique found no route to byte loss, and its four blocking findings are settled in D1, D2 and D4 (consume artifacts only, recover through the Source, record local stages at finish, pin page identity, plan pages in order).

## 2. Stage manifests and consuming

- [x] 2.1 Add a member manifest (stage-relative path, size, SHA-256, hashed while unpacking) to the Cloud import receipt for an `adopt` import, and remove it on discard. Verify: tests show the receipt lists exactly the staged files and a discard removes it.
- [x] 2.2 Write the local stage record from the journal when `exomem import` finishes; write none for an open import or an unsettled state placement, and report that. Verify: tests show the record after finish, none while open, and a rerun of an open import copies only missing files.
- [x] 2.3 Record the stage and its proof at `start`, and add `consume=false`. Verify: tests show a manifest-backed stage recorded and a hand-made `_Imports/<id>/` folder not recorded.
- [x] 2.4 After a chunk commits, remove each applied artifact's staged copy through `held_fs.unlink` with an identity check, when the durable copy and the manifest entry both match. Report `consumed`. Verify these with tests:
  - artifacts lose their staged copies only after their Knowledge Base copies match;
  - text items and opted-out runs keep theirs;
  - a file added to a stage after its manifest is never removed;
  - a failed removal leaves the item applied and not consumed.
- [x] 2.5 Resolve a missing staged copy in a recorded stage through the import index before failing, in `apply`, `retry_failed`, the repeat-call re-verification and `probe_staleness`. Verify: tests show a stop after commit and before the run save recovering with no failure, and a second run over the same stage resolving consumed items as already applied.
- [x] 2.6 Make `finish` remove empty stage folders and report the residual with counts, bytes and the discard route. Verify: a test shows unselected, ineligible, failed and text items in the residual.

## 3. Scale

- [ ] 3.1 Add the opaque `after` cursor and `next_after`, pin the collection root and label from the first page, and order artifacts before text. Verify: a test pages a flat folder of more than `max_candidates` files and covers each exactly once under one identity, also after consuming changed the stage's top level.
- [ ] 3.2 On a paged collection, resolve links by path or by a bare name unique across the whole scan, leave alias-only matches unresolved, and refuse to plan a page while an earlier page has unapplied items. Verify: tests show an ambiguous name left unresolved, a unique one resolved, and the refusal naming the earlier run.
- [ ] 3.3 Add `adoption_ceiling()` for staged files on every runtime, leaving `fetch_budget()` alone. Copy each artifact in one streamed pass that hashes while it copies and rolls back on a mismatch. Size the default to one call's time budget, and leave a larger file in the residual with a code. Verify: a Cloud-mode test applies a staged artifact over 100 MiB with memory bounded by the chunk size, and a mismatch rolls back.
- [ ] 3.4 Check free space plus a reserve that covers derived state before each chunk, and stop with a resumable `INSUFFICIENT_SPACE`. Verify: a test with a constrained free-space reading stops before the chunk and resumes.

## 4. Agent guidance

- [ ] 4.1 Update the `observe_transfer` description, the `import_handoff` routes and the `adoption_studio` next actions to name the whole flow, including the next page and the residual review. Add a generic import reference to the skill scaffold. Regenerate the tool-surface contract and the plugin packages. Verify: the tool-surface and scaffold-leak tests pass, and the next actions name `next_after` while it is set.

## 5. Proof

- [ ] 5.1 Extend the Cloud rehearsal import scenario on a local-storage cell with a fixed volume below twice the data. Use a generated tree of about 70,000 files: small Markdown files with links, one flat folder of more than 5,000 attachments, files over 100 MiB, a `.git` folder and hidden files. Run, through the MCP client: CLI import, every adoption page to `finish`, discard and the index build. Then import a second, media-heavy folder into the same cell. Verify:
  - the run recorded the stage's manifest proof;
  - every eligible file in the generator's manifest has a Source whose hash matches;
  - the residual equals the expected set;
  - no write fails for lack of space;
  - the report gives the peak volume use as a lower bound, the cell's peak memory during the index build, and the time per call of `read_import_index` at the final Source count.
- [ ] 5.2 Run the full corpus at the batch boundary, run the independent review, and merge.
