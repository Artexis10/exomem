## 1. OCR text availability

- [x] 1.1 Carry the measurement in the engine label and project it from released provenance. Verify retained OCR, tags alone, and the unchanged raw response and content hash.
- [x] 1.2 Verify that pending results retain their compatible shape and that reprocessing replaces the current marker and preserves the earlier marker.

## 2. Installed acceptance and closure

- [ ] 2.1 On an isolated installation, verify `read_page` reports `true` for retained OCR text, `false` for tags alone, and omits the field on a pre-existing sidecar. Verify the tags-only sidecar reports completion and is searchable by tag. Verify the prior runtime can finish a marked handoff and reprocess the completed sidecar. Verify the current runtime reads the archived measurement and omits the field on the replacement.
- [ ] 2.2 After installed acceptance, synchronize the delta and archive the change. Verify with `openspec validate --all --strict` before and after archiving.
