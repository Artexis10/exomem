# Tasks: ingest-attachments-as-sources

## 1. Citation resolution

- [x] 1.1 Test that a note citing a stored artifact path receives the back-reference on that artifact's page, for a media artifact and for a text artifact, with no source-not-found warning.
- [x] 1.2 Test that a note citing the page path directly resolves to the same page, and that an ordinary `.md` source page citation resolves exactly as before.
- [x] 1.3 Test the ordering explicitly: assert that a path whose extension-replaced twin does not exist resolves through the `<path>.md` form, so a future reordering that puts extension replacement first fails.
- [x] 1.4 Extend `note._resolve_source_path` to try the artifact's own page before extension replacement, and `<stem>-notes.md` first for a citation already ending `.md`. Apply it to both back-reference sites in `note.py`.
- [x] 1.5 Verify the same resolution is used by the compile-proposal source lookup, or record why that surface differs. Measured: it differs and is already correct — `compile_proposal` appends `.md` to the normalized wikilink instead of replacing the extension, so it resolves both an artifact path and its page today. No change needed; the divergence is worth leaving because `note.py` must also handle the `<stem>-notes.md` form, which that surface never sees.

## 2. Every ingested artifact is addressable

- [x] 2.1 Test that a text artifact stored with no description and no extracted text still receives a page carrying `type: source`, a stable `exomem_id`, empty `ingested_into`, the artifact pointer, original filename, SHA-256, and byte count.
- [x] 2.2 Test that the media stub is unchanged: media type declared, extraction pending, and the extraction anchor present, with the worker still converging on that page.
- [x] 2.3 Test that the page body is non-empty and describes the artifact, so `schema.validate_source`'s content requirement is satisfied without inlining bytes.
- [x] 2.4 Make the page unconditional in `preserve()`; synthesize the body from artifact metadata when there is no description and no text.
- [x] 2.5 Re-aim `tests/test_preserve.py`'s `sidecar_path is None` assertion onto the property that survives: no *empty* page is written, and the page carries the addressing contract. Do not delete it.

## 3. Page ownership is separated from the media pipeline

Measured blocker, found while implementing group 4 and not anticipated by the
first design pass: `reconcile_media` re-renders any media page that is not in its
canonical pending shape, and that shape requires `source_type: other`. A Source
page for an image loses its title, kind, domain, projects, and tags, and has its
body demoted under `## Preserved notes`. This group must land before a Sources
media capture is safe to offer.

- [x] 3.1 Test that reconciling a media page carrying a real `source_type`, `domain`, `projects`, title, and body leaves all of them intact while still filling the media fields.
- [x] 3.2 Test that the Evidence stub path is unchanged: a page already in the canonical pending shape converges exactly as it does today.
- [x] 3.3 Narrow the canonical-shape checks to the fields the pipeline owns — `media_type`, `evidence_file`, `extracted_by`, `processing_state`, and binary provenance — rather than the whole frontmatter.
- [x] 3.4 Re-render by editing the owned fields in place instead of rebuilding the page, so identity, classification, and body survive.
- [x] 3.5 Replace the positional `evidence`-segment derivation of scope and category with a lane-aware one, in both `reconcile_media` and `ensure_media_sidecar`; test the Sources case.

## 4. Sources lane ingestion

- [x] 4.1 Test that `capture_source` with a text file handle stores bytes under `Sources/`, applies the supplied kind, domain, and projects, and passes no base64 through a model-visible argument.
- [x] 4.2 Test that `capture_source` with an image file handle produces a media page awaiting extraction, and that the existing extraction path converges on it without destroying its classification.
- [x] 4.3 Test that the artifact and its page share one resolved stem, that naming authority is `add`'s uniquify rather than an `ARTIFACT_EXISTS` refusal, and that the original filename is recorded in frontmatter rather than used as the path.
- [ ] 4.4 Test that a byte-identical retry within the replay window returns the cached terminal result and does not write a second uniquified copy.
- [ ] 4.5 Test that safe-fetch behaviour is shared, not re-implemented: a private-address URL, an oversized body, and an expired handle each fail through the Sources path with the same stable codes as the Evidence path.
- [x] 4.6 Add `files` to `capture_source`, reusing the existing staging function and the existing file-handle type; persist through a Sources destination that reuses `add`'s taxonomy, indexes, and log writes.
- [x] 4.7 Test that `preserve_artifacts` and `preserve_evidence` are unchanged: same destination, same per-file outcomes, same failure codes.

## 5. The lane is stated, never inferred

- [x] 5.1 Test that the same bytes reach different lanes purely by which command was called, for a text file and for an image.
- [x] 5.2 Test that no lane decision reads MIME type, filename, or extension — assert by capturing an image through the Sources command and a `.md` file through the Evidence command.
- [ ] 5.3 Test that a minted upload capability names its lane and that bytes posted against it land in that lane.
- [ ] 5.4 Carry the lane on `transfer_artifact` and `/upload`, fixing the destination when the capability is minted rather than when the bytes arrive.

## 6. Promotion and Evidence semantics are untouched

- [x] 6.1 Test that Sources-to-Evidence promotion still refuses without a reason and that the reverse move is still refused, including for an artifact captured through the new Sources path.
- [ ] 6.2 Test that a Source captured as an attachment can be promoted, and that both its bytes and its page arrive in Evidence with the pointer still correct. **Measured gap:** `move_file` moves one file, so promotion currently relocates the bytes and leaves the page in `Sources/` with a dangling `evidence_file` — the bytes arrive with no page at all. An artifact and its page are one unit; the move must relocate both and rewrite the pointer in one atomic batch, keeping the promotion-reason requirement and the one-way refusal.
- [x] 6.3 Fix `review_context`'s evidence-field grouping so a Source page carrying the artifact pointer is not presented under an evidence heading; test both lanes.

## 7. Guidance and surface

- [ ] 7.1 Update scaffold guidance to select the lane before the transport, keeping it generic; `tests/test_scaffold_no_leak.py` must stay green.
- [ ] 7.2 Update the bootstrap contract and tool descriptions to state the same intent-first routing.
- [ ] 7.3 Regenerate the tool surface with `scripts/dump-tool-schemas.py` and review the diff for exactly the intended additions.
- [ ] 7.4 Confirm the ChatGPT Personal Plugin attestation is not silently refreshed, and record the fingerprint change as release-blocking for that consumer.

## 8. Back-fill

- [ ] 8.1 Test that an artifact stored before this change gains a page and becomes citable without moving lane.
- [ ] 8.2 Extend the existing media back-fill to non-media artifacts, idempotently.

## 9. Verification

- [ ] 9.1 Test the ingestion matrix end to end across text, image, PDF, audio, and video, in both lanes.
- [ ] 9.2 Test through a generic MCP client, not only the in-process path, so the file-handle parameter is exercised as clients actually send it.
- [ ] 9.3 Assert no byte duplication: one stored copy per captured artifact, and no artifact written to both trees.
- [ ] 9.4 Run `openspec validate ingest-attachments-as-sources --strict` and `openspec validate --specs --strict`.
- [ ] 9.5 Run the affected suites, plus lint, plus the tool-surface fidelity test.

## 10. Closure

- [ ] 10.1 Once this change is merged and therefore demonstrably shipped, sync its deltas into `openspec/specs/` and archive it with `openspec archive` in the same delivery, re-running `openspec validate --all --strict` before and after. Left open deliberately: archiving before the merge would claim a shipped state that does not exist yet, and the archive-discipline check treats a fully-ticked active change as debt.
