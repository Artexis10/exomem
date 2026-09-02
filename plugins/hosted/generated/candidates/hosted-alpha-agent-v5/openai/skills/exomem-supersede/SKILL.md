---
name: exomem-supersede
description: Record intent before the work, correct a page in place, and supersede a conclusion that has actually changed.
required_tools: [read_memory, plan_memory, edit_memory, replace_memory]
---

# Intent, correction, and supersession

Memory that only ever grows cannot be trusted: from the outside, the newest
conclusion and the stalest one look identical. Three moves keep it honest.

## State the intent before the work

When the user commits to a direction that outlives this conversation -- an
approach, a migration, a decision they mean to act on -- record it with
`plan_memory` before the work, not as a summary afterwards. A recorded intent is
what a later session compares the actual outcome against.

## Correct in place when the conclusion still holds

If the conclusion is right and only the wording, one field, or one section is
wrong, fix the page with `edit_memory` and an honest one-line rationale. A small
correction does not deserve a second page.

## Supersede when the conclusion itself changed

If the finding has actually changed -- new evidence, a reversed decision, a
result that did not reproduce -- do not write a second page that quietly
disagrees with the first. Read the current page with `read_memory`, then
supersede it using `replace_memory`, naming what changed and why. The old page
stays readable and points forward, so the history of the belief survives.

Supersession is deliberately a two-step move. Preview it first; the preview
returns the exact review values the commit requires. Then commit the identical
content with those values echoed back unchanged. If the preview and the commit
disagree, the commit is refused rather than guessed -- preview again against the
page as it stands now.

## Boundaries

- Never supersede a Source or an Evidence artifact. Raw material is append-only;
  supersede the conclusion drawn from it instead.
- Never supersede a page you have not read in this conversation.
- A disagreement you have not verified is a reason to ask, not a reason to
  revise.
