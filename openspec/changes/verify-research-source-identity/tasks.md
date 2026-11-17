## 1. Identity evidence at capture

- [ ] 1.1 Add `document_identity.py` with the pure check, the DOI-syntax expression and its one-line reason, and safe XMP parsing.
- [ ] 1.2 Read a captured PDF's first two pages, document information and XMP at capture. Record `identity_check` on the Source and return the `identity` row block.
- [ ] 1.3 State `conflict` and `not_shown` in the outcome sentence, and name the correction call in `next_action`.
- [ ] 1.4 Recompute the check after OCR for `unavailable` with reason `no_text_layer`. Keep `identity_check` and `document_form` through media processing.
- [ ] 1.5 Declare `defusedxml` in the `media-cpu` extra.

## 2. Document form

- [ ] 2.1 Ship `vocabulary/packs/core/document-forms.yaml` with its registry spec, vault overlay and auto-registration.
- [ ] 2.2 Accept `document_form` on `save_source` and the capture leaf, and render it on the Source page.

## 3. Correction and search

- [ ] 3.1 Accept a metadata-only correction that reuses the predecessor's original after its fixity check, and skip duplicate reuse for corrections.
- [ ] 3.2 Carry the predecessor's `ingested_into` to the successor.
- [ ] 3.3 Add `corrected_by` to Source search results through the correction resolver.

## 4. Audit

- [ ] 4.1 Add the `source_identity_conflict` audit family with the correction call as its proposed fix.

## 5. Guidance

- [ ] 5.1 Add the research-source lines to the served capture guidance and the scaffold.
- [ ] 5.2 Remove each line that does not beat the arm without it in the real-model comparison.

## 6. Proof

- [ ] 6.1 Add deterministic tests: each identity state, a citing work, an unreadable PDF, a metadata-only correction, a changed original, an undo, a duplicate re-capture, `corrected_by` and the audit family.
- [ ] 6.2 Time PDF capture before and after on 1 MB, 20 MB and 300-page generated PDFs.
- [ ] 6.3 Add the research-provenance incident cases and the `source_saved` and `source_field` checks. Run base against changed on real models and record the per-case result.
- [ ] 6.4 Regenerate derived artifacts and run the public-artifact privacy gate.
