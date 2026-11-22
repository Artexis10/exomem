## 1. Served text

- [x] 1.1 Add `_CAPTURE_RULES` and serve it as `rules` in the activation `capture_policy` block at every disposition except `off`.
- [x] 1.2 Replace `_EPISTEMIC_CAPTURE` with a one-sentence form of the rule. Drop "tentative events" from the `_BASELINE_CAPTURE` tail.
- [x] 1.3 Render `rules` after `say` in the hook, under its own cap, and omit it whole when it is over the cap.
- [x] 1.4 Count and reserve the block without `rules` on the server, and render `rules` on its own budget in the hook, so it takes no map room and no working-set room.
- [x] 1.5 Measure the longest `say` rendering, the `rules` length and the populated core at `maximal` on every surface.

## 2. Scaffold and docs

- [x] 2.1 Align `_Schema/SKILL.md`, `references/engagement.md`, `references/operations.md` and the `exomem-capture` and `exomem-reflect` workflow skills to the rule.
- [x] 2.2 Drop "tentative events" from the copyable instruction blocks and update the measured-size pins.
- [x] 2.3 Regenerate the skill contract stamp, the packaged skills and the cloud plugin with their generators.

## 3. Proof

- [x] 3.1 Test that every non-`off` block carries `rules`, that the hook renders `say` and `rules` within their caps, and that an over-cap `rules` is dropped while `say` is kept.
- [x] 3.2 Test that the first request's map does not shrink with the `rules` text, and that the hook shows the whole working set beside `rules`.
- [x] 3.3 Change the tentative-affiliation fixture case to a compiled observation and keep its mutation checks.
- [x] 3.4 Run the real-model probe on the unsure-report, held-view, open-question and settled-value cases and on a trivia control. Record the per-case result against the base text. Done on 12 unseen blind cases, 2 repeats, with and without a hook. Right saves rose from 13 of 32 to 26 of 32, and no write was stronger than its source. Session recaps named incidental material in 4 of 16 control runs against 1 of 16 on the base text; the maintainer chose to ship on that result.

## 4. Release

- [x] 4.1 Independent review, merge and release. Merged as #2985, released in 0.196.0, verified on the installed service 2026-11-22.
- [x] 4.2 Archive this change with `openspec archive` after the release. Archived in this PR.
