## 1. Served text

- [x] 1.1 Change `_BASELINE_CAPTURE` to "tentative events stay quiet". Replace the `balanced` "unresolved questions" exclusion with the open-question clause.
- [x] 1.2 Add `_EPISTEMIC_CAPTURE` and serve it in the activation `capture_policy` block.
- [x] 1.3 Measure the block at every non-`off` disposition and link-acceptance value against the hook's 1,600-character cap. Measure the core headroom at every level and surface.

## 2. Scaffold

- [x] 2.1 Add the three classes to `_Schema/SKILL.md`, `references/engagement.md` and the `exomem-capture` and `exomem-reflect` workflow skills, outside the personal-baseline sections. Change those sections only by the "tentative events" swap.
- [x] 2.2 Swap "tentative claims stay quiet" for "tentative events stay quiet" in the copyable instruction blocks, and drop the `balanced` block's "or open questions" exclusion.
- [x] 2.3 Regenerate the derived skill copies and the cloud plugin with their generators.

## 3. Proof

- [x] 3.1 Add one test: the block carries the sentence and stays ASCII at every non-`off` disposition and link-acceptance value. Show it fail on an over-long sentence.
- [x] 3.2 Run the real-model probe on the three classes and the control (a casual tentative personal claim). Record the per-case result against the base text.
- [x] 3.3 Run the scoped suites, `openspec validate --all --strict` and the public-artifact privacy gate.

## 4. Release

- [x] 4.1 Independent review, merge and release. Merged as #2953, released in 0.194.0, verified on the installed service 2026-11-21.
- [x] 4.2 Archive this change with `openspec archive` after the release. Archived in this PR.
