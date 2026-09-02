## 1. Held-filesystem generation tokens

- [ ] 1.1 Add a held-filesystem capability probe that captures durable monotonic namespace-generation tokens for both retained parents, restart-comparable and non-repeating within their filesystem epoch, changing for every namespace-entry mutation including rename followed by inverse rename.
- [ ] 1.2 Add red tests for the reset, unavailable, non-monotonic, cross-epoch, and incomparable token cases, and for conservative refusal on unrelated entry mutation.

## 2. Prepared-relocation adapters

- [ ] 2.1 Add prepared-relocation adapters and parity tests for governed `move`, `delete`, and `recover`, including existing-parent refusal, canonical delete confirmation, exact trash identity, source stable identity, complete auxiliary-write manifests, graph/lifecycle identities, durable parent-namespace generation tokens, ordered candidate/authorization file-and-parent durability, exact presealed witness bytes, both-parent flush, and no recovery-issued rename from exact-target placement.

## 3. Relocation fault barriers and recovery

- [ ] 3.1 Add deterministic `BaseException` fault barriers after each retained-parent preflight flush; after candidate-file flush, every newly created governed-run ancestor-entry flush, and candidate containing-parent flush; after authorization-file flush and containing-parent flush; after rename before parent flush, after each distinct parent flush, every auxiliary-write prefix, and graph/lifecycle finalisation, final-witness file flush, and witness-parent flush.
- [ ] 3.2 Add crash/restart tests at every relocation barrier proving each operation has at most one durable/adopted placement even when an exact-prior proof permits a second rename syscall after power loss, and that exact-target recovery adopts the bound desired placement and issues no rename.

## 4. Closure

- [ ] 4.1 Replace `add-governed-curation-lane`'s v1 fail-closed relocation requirement only after the protocol is proven end to end, and run `openspec validate --all --strict` before and after.
