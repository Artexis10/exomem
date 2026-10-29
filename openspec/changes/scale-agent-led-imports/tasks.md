## 1. Design gate

- [ ] 1.1 Run an independent critique of `design.md`, D1 to D7, and settle its four open questions in the design. Verify: the critique's blocking findings are resolved or withdrawn with evidence.

## 2. Consuming recorded staged copies

- [ ] 2.1 Record the stage and its proof at `start`: a Cloud receipt for an `adopt` import, or a local stage record. Add `consume=false`. Verify: tests show a receipt-backed stage recorded, a hand-made `_Imports/<id>/` folder not recorded, and an opted-out run recorded as not consuming.
- [ ] 2.2 Write the local stage record when `exomem import` claims an import id, and report when it cannot. Verify: a test shows the record in an initialized vault and no record, with the report, when the state placement is not settled.
- [ ] 2.3 Consume after a verified commit, in this order: pending, then unlink through the held filesystem, then done. Verify these with tests:
  - an applied artifact and an applied text item lose their staged copies only after their durable copies verify;
  - an item still owed a note keeps its copy until the note settles;
  - a simulated stop after commit and before unlink recovers on the next `apply`, with no failure;
  - a folder without a stage record keeps every original byte-identical;
  - repeat-call verification and `probe_staleness` skip consumed items.
- [ ] 2.4 Make `finish` remove empty stage folders and report the residual with counts, bytes and the discard route. Verify: a test shows the residual for unselected, ineligible and failed items.

## 3. Scale

- [ ] 3.1 Add the `after` cursor and `next_after` to `start`. Verify: a test pages a flat folder of more than `max_candidates` files and covers each exactly once, in order.
- [ ] 3.2 Use the upload-session cap as the staged-file ceiling on every runtime. Read, hash and copy artifacts in bounded chunks, and turn an over-ceiling text file into an artifact item. Verify: tests show a staged file over 100 MiB eligible and applied in Cloud mode, with peak memory bounded by the chunk size, not the file size.
- [ ] 3.3 Check free space before each chunk and stop with a resumable `INSUFFICIENT_SPACE`. Verify: a test with a constrained free-space reading stops before the chunk and resumes.

## 4. Agent guidance

- [ ] 4.1 Update the `observe_transfer` description, the `import_handoff` routes and the `adoption_studio` next actions to name the whole flow, including the next page and the residual review. Add a generic import reference to the skill scaffold. Regenerate the tool-surface contract and the plugin packages. Verify: the tool-surface and scaffold-leak tests pass, and the next actions name `next_after` while it is set.

## 5. Proof

- [ ] 5.1 Extend the Cloud rehearsal import scenario. Use a generated tree of about 70,000 files: small Markdown files with links, one flat folder of more than 5,000 attachments, files over 100 MiB, a `.git` folder and hidden files. Use a volume smaller than twice the data. Run, through the MCP client: CLI import, every adoption page to `finish`, discard and the index build. Then import a second, media-heavy folder into the same cell. Verify:
  - every eligible file in the generator's manifest has a Source whose hash matches;
  - the residual equals the expected ineligible set;
  - no write fails for lack of space;
  - the report gives the peak volume use as a lower bound and the cell's peak memory during the index build.
- [ ] 5.2 Run the full corpus at the batch boundary, run the independent review, and merge.
