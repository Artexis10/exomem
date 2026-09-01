## 1. Red-first adoption fixtures

- [ ] 1.1 Add generic draft, selection, rejection, approval, and delivery lifecycle fixtures with explicit zero-write expectations for unadopted variants.
- [ ] 1.2 Add exact-byte, digest, size, content-type, media-identity, and receipt assertions for synthetic image, PDF, audio, video, slide, spreadsheet, and text outputs.
- [ ] 1.3 Add wrong-variant, textual reconstruction, missing-receipt-field, token-only success, and artifact-plus-receipt atomicity failure tests.
- [ ] 1.4 Add transport replay and durable cross-session adoption-key tests with fetch and canonical-write counters, including changed bytes and expired/unavailable handles.

## 2. Preservation leaf and receipt

- [ ] 2.1 Add and validate the optional single-artifact adoption envelope on `capture_source` and `preserve_artifacts` without changing either command's ordinary behavior.
- [ ] 2.2 Extend the Source artifact page and Evidence companion vocabularies with the same versioned vault-owned adoption receipt and keep every renderer and media reconciler lossless for it.
- [ ] 2.3 Implement transport-window no-fetch replay, then durable receipt lookup plus re-stage/hash comparison beyond the window; return the original receipt on exact identity, `ADOPTION_KEY_REUSED` on mismatch, and `ADOPTION_REPLAY_UNVERIFIABLE` when bytes cannot be reproved.
- [ ] 2.4 Publish adoption inputs and bounded committed/replayed/failed/unverifiable/handoff outcomes for both lanes through MCP, CLI, REST/OpenAPI, compact projections, and generated tool contracts.
- [ ] 2.5 Add Source/Evidence parity tests proving identical bytes and receipt fields, lane-specific canonical pages, exact lane preservation, and no cross-lane duplicate from one adoption request.

## 3. Delivery and active-agent behavior

- [ ] 3.1 Add scaffold, bootstrap, workflow, prominence, and documentation guidance for draft silence, explicit adoption, semantic destination, exact-byte receipts, and separate delivery.
- [ ] 3.2 Add real existing-collection Records integration tests for receipt-gated delivery links, reported versus verified remote identity, and no implicit collection creation.
- [ ] 3.3 Add capability-sensitive no-handle behavior that reports unavailable or non-committing handoff without false save language.
- [ ] 3.4 Add delegation-envelope and carrier tests proving selection/approval is an adoption fact, not write confirmation: Source/Evidence adoption and delivery Records obey `proactive_capture`, explicit save remains requested action, and missing-collection proposals/changes stay structural and confirmed.

## 4. Hosted v5 candidate

- [ ] 4.1 Add the generic synthetic artifact contribution input at `tests/fixtures/hosted_v5_contributions/artifact_adoption.json`; do not edit, render, lock, archive, promote, or roll back v5 files from this lane.
- [ ] 4.2 Require the single v5 owner to validate, canonicalise, and freeze the artifact input into the candidate-owned combined fixture while preserving v4 command order and withholding `transfer_artifact` until a real bridge exists.
- [ ] 4.3 Prove the shared combined fixture digest includes artifact behavior alongside baseline, recurring-entity, and governed-curation behavior and is bound by the owner's compatibility, package, lock, archive, and promotion gates; fixture drift MUST invalidate package verification.
- [ ] 4.4 Supply clean supported-client traces for Source and Evidence direct adoption, sibling no-write, no-handle handoff, receipt-linked delivery, and no false remote-byte claim before owner render/promotion.
- [ ] 4.5 Verify pre-lock rebuild and post-promotion v6-only correction semantics through the owner's shared-v5 rollback tests.

## 5. Mutants and verification

- [ ] 5.1 Kill mutants that remove explicit selection, swap the selected id, reconstruct bytes, omit receipt fields, or mark handoff committed.
- [ ] 5.2 Kill mutants that disable request-window replay, skip later re-stage/hash comparison, accept an unverifiable expired handle, lose Source/Evidence receipt parity, record delivery before receipt, or assert remote identity without platform proof.
- [ ] 5.3 Run focused preservation, Records, carrier, Hosted package/promotion, public-input, scaffold leak, generated-contract, and historical-lock tests.
- [ ] 5.4 Run `ruff check`, the full non-model pytest suite, and `openspec validate --all --strict` and report exact false-positive and draft-clutter counts.

## 6. OpenSpec closure

- [ ] 6.1 After shared v5 delivery and all merge/review evidence are complete, archive only through the owner-controlled order defined by `capture-durable-personal-baselines`, with strict validation before and after this change's archive.
