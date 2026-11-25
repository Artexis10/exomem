# Tasks

## 1. Instructions

- [x] 1.1 Replace the omission rules in the scaffold references, the `evermatter-capture` skill, the upload route strings, the upload page and the activation learning instruction. Agents keep identity, health and financial material in full, and only authenticators stay excluded. Regenerate the plugin copies and refresh the skill contract stamp.

## 2. Registry and field

- [x] 2.1 Add the `sensitivity-classes` registry: module, shipped pack, `default_disclosure` validation, `propose-save`, and listing in `registry_specs()`.
- [x] 2.2 Declare `sensitivity` in the `page-fields` pack as a list pointer field, with the new closed writer value `classify`.
- [x] 2.3 Add the optional `sensitivity` attribute to `source-kinds` entries.

## 3. Capture and classify

- [x] 3.1 Accept `sensitivity` on `save_source`, `save_evidence`, `create_note`, `save_episode` and `add_record`. Apply the kind default to Source and Evidence captures. Refuse unknown classes with `SENSITIVITY_CLASS_UNKNOWN`.
- [ ] 3.2 Report `sensitivity {classes, basis, disclosure}` in every capture and classify result. `disclosure` comes from live governance state.
- [ ] 3.3 Add `manage_memory(operation="files.classify")`, with preview, required reason and revert through the governed pointer write. The file never moves.
- [x] 3.4 Collection-level `sensitivity` sets a floor that a row may extend and never shrink. Store it in the row governance metadata.

## 4. Proof and delivery

- [ ] 4.1 Run one real-model end-to-end check on a temporary vault with synthetic identity-card images:
  - The agent keeps them in full and classifies them without asking for a covered copy.
  - Classify and revert leave the image bytes and the page body unchanged (sha256 before and after).
  - Owner recall returns the synthetic document number.
- [ ] 4.2 Run the full suite, `openspec validate --all --strict` and the privacy gate. Obtain two independent reviews, merge, and verify the ordinary release.
