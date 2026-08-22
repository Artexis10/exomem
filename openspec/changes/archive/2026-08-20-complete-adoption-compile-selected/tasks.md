# complete-adoption-compile-selected - tasks

## 1. Adoption Behavior

- [x] 1.1 Add a small context reference helper for stable `exomem://` refs.
- [x] 1.2 Implement `adopt(mode="compile-selected")` using explicit `selected_paths`.
- [x] 1.3 Reuse copy-as-sources for outside-KB legacy text files and plan already-governed Sources directly.
- [x] 1.4 Return additive refs and compile proposal metadata without creating compiled notes.

## 2. Surfaces and Docs

- [x] 2.1 Update `op_adopt` tool docs and any schema fixture affected by that public description.
- [x] 2.2 Update setup wizard text to name manifest review, source copy, and compile-selected planning.
- [x] 2.3 Update product/quickstart docs so `compile-selected` is implemented as planning, not automatic migration.

## 3. Verification

- [x] 3.1 Add adoption and context-ref tests for compile-selected behavior and non-destruction.
- [x] 3.2 Run focused tests for adoption, compile proposals, setup wizard, and schema fidelity if needed.
