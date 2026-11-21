## 1. Served text

- [x] 1.1 Add `_CAPTURE_RULES` and serve it as `rules` in the activation `capture_policy` block at every disposition except `off`.
- [x] 1.2 Replace `_EPISTEMIC_CAPTURE` with a one-sentence form of the rule. Drop "tentative events" from the `_BASELINE_CAPTURE` tail.
- [x] 1.3 Render `rules` after `say` in the hook, under its own cap, and omit it whole when it is over the cap.
- [x] 1.4 Measure the longest `say` rendering, the `rules` length and the populated core at `maximal` on every surface.

## 2. Scaffold and docs

- [x] 2.1 Align `_Schema/SKILL.md`, `references/engagement.md`, `references/operations.md` and the `exomem-capture` and `exomem-reflect` workflow skills to the rule.
- [x] 2.2 Drop "tentative events" from the copyable instruction blocks and update the measured-size pins.
- [x] 2.3 Regenerate the skill contract stamp, the packaged skills and the cloud plugin with their generators.

## 3. Proof

- [x] 3.1 Test that every non-`off` block carries `rules`, that the hook renders `say` and `rules` within their caps, and that an over-cap `rules` is dropped while `say` is kept.
- [x] 3.2 Change the tentative-affiliation fixture case to a compiled observation and keep its mutation checks.
- [ ] 3.3 Run the real-model probe on the unsure-report, held-view, open-question and settled-value cases and on a trivia control. Record the per-case result against the base text.

## 4. Release

- [ ] 4.1 Independent review, merge and release.
- [ ] 4.2 Archive this change with `openspec archive` after the release.
