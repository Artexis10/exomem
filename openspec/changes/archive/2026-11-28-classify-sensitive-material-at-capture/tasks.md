# Tasks

## 1. Instructions

- [x] 1.1 Teach agents to keep identity, health and financial material in full in the scaffold references, the `evermatter-capture` skill, the upload route strings, the upload page and the activation learning instruction. Only authenticators stay excluded, named as such. Regenerate the plugin copies and refresh the skill contract stamp.

## 2. Registry and field

- [x] 2.1 Add the `sensitivity-classes` registry: module, shipped pack of fixed label entries, `propose-save`, and listing in `registry_specs()`.
- [x] 2.2 Declare `sensitivity` in the `page-fields` pack as a list pointer field, with the new closed writer value `classify`.
- [x] 2.3 Add the optional `sensitivity` attribute to `source-kinds` entries.

## 3. Capture and classify

- [x] 3.1 Accept `sensitivity` on `save_source`, `save_evidence`, `create_note`, `save_episode` and `add_record`. Apply the kind default to Source and Evidence captures, skipping unregistered default keys. Refuse unknown declared classes with `SENSITIVITY_CLASS_UNKNOWN`, also on a plain edit of a compiled page.
- [x] 3.2 Report `sensitivity {classes, basis, disclosure}` in every capture and classify result. `disclosure` comes from live governance state.
- [x] 3.3 Add `manage_memory(operation="files.classify")`, with preview, required reason and revert through the governed pointer write. The file never moves.
- [x] 3.4 Collection-level `sensitivity` sets a floor that a row may extend and never shrink. Store it in the row governance metadata.

## 4. Proof and delivery

- [x] 4.1 Run the real-model incident replay on the delivered instructions, against main as the baseline, one draw each. Compare the arms on what the agent did: the judge check and the recall check. The class check runs on the changed arm only, because main cannot write the field.
  - Synthetic identity-card photos: the agent keeps them in full and classifies them without asking for a covered copy.
  - A synthetic payment-card photo: the agent keeps no card number and asks for a covered copy.
  - A synthetic identity scan with a text layer: a later owner session reads the document number back. Reading a number from a photo needs server OCR, which this change does not add.
- [x] 4.3 Prove classify and revert change only the page's `sensitivity` field and restore its exact bytes (`tests/test_sensitivity_classification.py`). Classify writes only the `.md` page, never a stored binary.
- [x] 4.2 Run the full suite, `openspec validate --all --strict` and the privacy gate. Obtain two independent reviews, merge, and verify the ordinary release. Evidence: two blind review passes approved the merged head; the merge queue run passed on main; release v0.209.0 (2026-11-28) contains the merge commit.
