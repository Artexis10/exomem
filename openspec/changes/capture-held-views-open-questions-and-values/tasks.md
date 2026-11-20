## 1. Served text

- [x] 1.1 Change `_BASELINE_CAPTURE` to "tentative events stay quiet". Replace the `balanced` "unresolved questions" exclusion with the open-question clause.
- [x] 1.2 Add `_EPISTEMIC_CAPTURE` and serve it in the activation `capture_policy` block.
- [x] 1.3 Measure the block at every non-`off` disposition and link-acceptance value against the hook's 1,600-character cap. Measure the core headroom at every level and surface.

## 2. Scaffold

- [x] 2.1 Align `references/operations.md`, `references/engagement.md` and the `exomem-capture` workflow skill with the spec.
- [x] 2.2 Regenerate the derived skill copies and the cloud plugin with their generators.

## 3. Proof

- [x] 3.1 Add one test: the block fits the hook's cap and carries the sentence at every non-`off` disposition and link-acceptance value. Show it fail on an over-long sentence.
- [ ] 3.2 Run the real-model probe on the three classes and the control (a casual tentative personal claim). Record the per-case result against the base text.
- [x] 3.3 Run the scoped suites, `openspec validate --all --strict` and the public-artifact privacy gate.

## 4. Release

- [ ] 4.1 Independent review, merge and release.
- [ ] 4.2 Archive this change with `openspec archive` after the release.
