## Why

Exomem never checks whether a page sits in the right project. The `project:` key is whatever the writing agent chose, homes route a page by that key without questioning it, and a curation split copies the key into every child. Material with its own subject can therefore pile up inside another project's home, unnoticed, until a person spots it.

An evaluation compared four ways to catch this: a server-side model sensor, a model-free selector feeding a judge, and the connected agent answering a spelled-out ownership question. A held-out run picked the last one. It beat the shipped path by +0.23 per case (Holm-adjusted 98.75% interval +0.08 to +0.39) with half a false alarm over 19 leave-alone cases and no repeated warnings, and an OpenAI-family client scored the same. Nearly all of the gain came from pages that already exist, where Exomem has no check today.

## What Changes

- Add the review family `ownership_review`: one item per eligible compiled page that asks the connected agent whether the page belongs in its current project, with the exact calls that answer it.
- Ship the ownership test as guidance in one place: a page belongs to a project when it serves that project's own goal; material with its own goal, institution or schedule is its own project, linked by a relation. The question also carries the deferral rule: an earlier decision stands while nothing that bears on ownership changes.
- Open an item when a page is created in a project, when its project or title changes, and through a bounded daily rotation over pages not yet asked. The rotation asks the pages that sit farthest from their project's centre first, and reaches every eligible page eventually.
- Serve open items through the due-state backlog, after dated obligations, with exact counts and a rotation progress count.
- Carry the question in the write response of a page whose item is open, and once per plan in a curation preview whose steps create or move notes.
- Answer through existing surfaces: a dismissal with an `intentional:` reason keeps the page where it is; a move, new project or split goes through the curation lane with the user's exact confirmation.

## Capabilities

### New Capabilities

### Modified Capabilities

- `attention-queue`: adds the `ownership_review` family, its due-state backlog, its rotation and its answers.
- `governed-curation-lane`: a preview whose steps create or move notes carries the ownership question once.

## Impact

- Code: a new family module beside `note_cohesion`, the due-state projection (new backlog category, schema version bump), the write-path structure suggestion and its terminal projector, the curation preview reply, the guidance route, and handling text in `commands.py`.
- Background work: the rotation's ordering needs per-project embedding centres, computed in a bulk refresh off the write path.
- Agent surface: one more `structure_suggestion` kind and one more review family; no new tool.
- Depends on `add-curation-relocation-protocol` for the `move` step with a `fields` patch, which the answer to a misfiled page uses.
