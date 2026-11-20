## 1. Identity evidence at capture

- [x] 1.1 Add `document_identity.py` with the pure check, the DOI-syntax expression and its one-line reason, and safe XMP parsing.
- [x] 1.2 Read a captured PDF's first two pages, document information and XMP at capture. Record `identity_check` on the Source and return the `identity` row block.
- [x] 1.3 State `conflict` and `not_shown` in the outcome sentence, and name the correction call in `next_action`.
- [x] 1.4 Recompute the check after OCR for `unavailable` with reason `no_text_layer`. Keep `identity_check` and `document_form` through media processing.
- [x] 1.5 Declare `defusedxml` in the `media-cpu` extra.

## 2. Document form

- [x] 2.1 Ship `vocabulary/packs/core/document-forms.yaml` with its registry spec, vault overlay and auto-registration.
- [x] 2.2 Accept `document_form` on `save_source` and the capture leaf, and render it on the Source page.

## 3. Correction and search

- [x] 3.1 Accept a metadata-only correction that reuses the predecessor's original after its fixity check, and skip duplicate reuse for corrections.
- [x] 3.2 Carry the predecessor's `ingested_into` to the successor.
- [x] 3.3 Add `corrected_by` to Source search results through the correction resolver.

## 4. Audit

- [x] 4.1 Add the `source_identity_conflict` audit family with the correction call as its proposed fix.

## 5. Guidance

- [x] 5.1 Add the research-source lines to the served capture guidance and the scaffold.
- [x] 5.2 Remove the guidance if the changed code with it does not beat the same code without it in the real-model comparison. The custody wording did not (no-nudge and screenshot cases, rounds 2 to 4) and is removed; the identity guidance did (misleading-DOI case) and stays.

## 6. Proof

- [x] 6.1 Add deterministic tests: each identity state, a citing work, an unreadable PDF, a metadata-only correction, a changed original, an undo, a duplicate re-capture, `corrected_by` and the audit family.
- [x] 6.2 Time PDF capture before and after on 1 MB, 20 MB and 300-page generated PDFs.
- [ ] 6.3 Add the research-provenance incident cases and the `source_saved`, `source_count`, `current_source` and `compiled_with_backlinks` checks. Run base against changed on real models and record the per-case result.
- [x] 6.4 Regenerate derived artifacts and run the public-artifact privacy gate.
- [x] 6.5 Name the DOI the document shows, and the document's own title, in the correction call on `conflict`. The 2026-11-20 independent acceptance found an agent that asked the person instead of correcting.
- [x] 6.6 Process a metadata-only correction's successor like any new Source, so it gets the document's text. On 2026-11-20 two live successors stayed `extracted_by: pending` with no job.
- [x] 6.7 Say in the comparison correction call that a correction keeps every field it omits and that `url ""` removes the url. On 2026-11-20 an independent acceptance run found an agent that meant to drop a DOI its document never showed, omitted `url`, and kept it.
