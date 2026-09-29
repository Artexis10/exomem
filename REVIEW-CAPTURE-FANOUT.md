# Review: feat/capture-fanout-and-self-entity (PR #1447)

Diff reviewed: `origin/integration/wave-bcd...c19ba23f`.

**Verdict: REQUEST_CHANGES.** The sink detector is correctly structural and
counted by distinct candidates, and the mirrors, stamp and capabilities are
clean. The per-turn byte growth and the unscoped routing of contact data still
need fixing before this merges.

## High

1. **The Stop ask always carries the sink text, even when there is no sink.**
   `src/exomem/_hooks/exomem_capture_nudge.py:155-157` (plugin mirror too).
   `COVERAGE_ASK` is static, so every coverage ask gets the sink sentence
   whether or not `coverage` reported a sink. It grows from 381 to 588
   characters (+207, +54%). That runs against the current work to cut injection
   bytes. It also breaks the spec's second scenario, "no sink … carries no sink
   guidance": the coverage pass follows it, the Stop ask doesn't. `sink_guidance`
   already arrives in the `coverage` response at exactly the moment it applies.
   Fix: drop the hook sentence and change the 4.6 wording from "and the Stop
   ask" to "through the coverage pass".

2. **Contact data gets routed with no audience guidance.**
   `src/exomem/_scaffold/_Schema/workflow-skills/exomem-capture/SKILL.md:73-77`,
   `src/exomem/capture_sweep.py:106-107`. Phone numbers and addresses, the
   owner's included, now go onto the person's entity under `proactive_capture`.
   Entity pages are the most widely resolved and linked pages, so they are what
   hosted candidates and restricted audiences are most likely to see.
   "Personal-details node" is defined nowhere: not in the page types, in
   governance, or in any spec. So the one place that could have been access-scoped
   doesn't exist, and an agent will default to the entity. The code path itself
   is fine: a withheld page returns `path=None` and never reaches `_sinks`
   (`episode_workflow.py:348-352`), so a sink can't reveal a hidden path. The gap
   is in the guidance. Fix: define the node (page type plus restricted-release
   handling), or say that contact details go only to a restricted node and never
   into the entity body.

## Medium

3. **Hub pages will be flagged as false sinks, and the text leans toward
   splitting them.** `episode_workflow.py:293-299`. The count is by distinct
   `candidate_key`, which is correct: one candidate with many effects never
   trips it. But a project hub, a log, or the owner's own entity will
   legitimately get three or more candidates in one busy episode, and item (b)
   now points more facts at that entity. The guidance says the page "received
   several distinct topic clusters" and to "prepare a corrective candidate for
   any cluster that does not belong here". That states a topical judgement the
   spec forbids ("SHALL NOT claim to judge topic similarity"), and "leave it
   here, it's the canonical page" isn't one of the listed exits. Fix: phrase the
   finding structurally ("received effects from N candidates"), add "keep here
   when this is the cluster's canonical page", and consider exempting
   `type: entity`/hub/log pages or carrying the page type.

4. **The 400-character cap on RULE is enforced nowhere.** `capture_sweep.py:102-108`.
   RULE goes from 299 to 389 characters (+90), with 11 left, and no test
   asserts `len(RULE) <= 400`. `CONSIDER` goes from 219 to 262 bytes as JSON
   (+43). Every sweep advisory carries +133 bytes. RULE and CONSIDER now also
   say the same thing twice, so one of them can go. Add a length assertion.

## Low

5. The spec says "several" and the code uses `SINK_CLUSTERS = 3`. Name the
   threshold in the requirement, or the next tweak will drift from it without
   anyone noticing.
6. The new tests only check phrases. Nothing covers one candidate with three
   effects on one page (the case that separates effects from candidates) or a
   withheld page among three. Both are cheap to add.

## Verified

- The hook and plugin mirror are byte-identical, and so is the capture
  `SKILL.md`. The `skill_contract` stamp is refreshed (`fb13de58…`) and
  consistent.
- `generate-capabilities.py --check`: current.
- Scoped pytest on the PR head (`test_episode*`, `test_capture*`,
  `test_scaffold*`, `*hook*`, `test_entity_capture_scaffold`): 585 passed,
  1 skipped. The one failure, `test_the_compact_bootstrap_budget_is_not_raised`,
  was a `tests` import error that only shows up when files are selected
  individually. It passes under `python -m pytest` alongside
  `test_bootstrap_compact_budget.py`. `test_scaffold_no_leak` passes.
- OpenSpec 4.6 and 4.7 match the code except for the Stop-ask clause (see 1).
- `gh pr checks 1447`: only "Conventional Commit title" ran (green, twice). The
  base is an integration branch, so the full CI suite didn't run on this PR.
