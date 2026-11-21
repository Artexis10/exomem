## 1. OCR text availability

- [x] 1.1 Carry the measurement through the job store to the page read. Verify both retained OCR and a tags-only result, and keep tags searchable.
- [x] 1.2 Keep legacy results readable and move an old measurement to the prior extraction page on reprocessing. Verify both cases in the OCR tests.

## 2. Installed acceptance and closure

- [ ] 2.1 On an isolated installation, verify `read_page` reports `true` for retained OCR text, `false` for tags alone, and omits the field on a pre-existing sidecar. Verify the tags-only sidecar reports completion and is searchable by tag. Follow the existing rollback procedure. Verify `result_publication.pending_count` reaches zero before downgrade and the previous runtime reads the committed sidecars.
- [ ] 2.2 After installed acceptance, synchronize the delta and archive the change. Verify with `openspec validate --all --strict` before and after archiving.
