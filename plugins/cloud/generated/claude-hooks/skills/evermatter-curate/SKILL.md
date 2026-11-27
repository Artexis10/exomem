---
name: evermatter-curate
description: 'Improve how Evermatter is organised: links, topics, titles, structure, and moving or splitting pages, through previewed plans. Use when the user asks to clean up, organise, link, restructure, rename or file notes.'
metadata:
  skill_contract: f6ea7d6fa01d99a9251274e1666272975b48464c06a54772adcbfec2143f32a6
  version: "0.2.0"
---

# evermatter-curate

This workflow builds on the `evermatter` skill. Its rules apply here and are not
repeated. If that skill is not loaded, load it; a client that cannot load it calls
`read_instructions(profile="compact")` for the portable rules. Before the first
compiled-note write of the session, read [the authoring contract](references/semantic-authoring.md);
the same file sits in the `evermatter` skill's folder, and `read_instructions(section="authoring")` serves it too.
Each call's fields are in the tool's description and in `read_instructions(topic=<tool or operation>)`.

## Purpose
Improve links, structure and note quality without changing what raw material says.

## When to use
The user asks to clean up, organise, link better, restructure, rename or file notes.

## Workflow
1. Check the `skills` that `recall_context` returned for an owner skill that covers this task. If one does, read it with `read_page` and follow its method through the steps below: it decides what to change and where it goes.
2. To organise by topic, read the topic-carrier items from `list_review_items(queue="vocabulary")` and follow "Topic carriers" in [vocabulary](references/vocabulary.md): one carrier save files every page of a project key, folder or tag. Do not edit pages one by one for this.
3. Find what to improve: `list_review_items(queue="unlinked")` for pages without edges, `list_review_items(queue="relation_suggestions")` for reviewed link candidates, and `inspect_memory(operation="connections.suggest_links", arguments={"path": "<page path>"})` (or `"draft_title"` and `"draft_body"` for an unsaved draft) or `connections.suggest_relations` for a page you are working on.
4. Accept a suggested relation with `decide_review_item(review_item=..., decision="accept", relation=<option key>)` after the user confirms it.
5. For a structural change to selected pages, get the context with `inspect_memory(operation="curation.work_item", arguments={"refs": [...]})`, author the plan from its `step_schemas` (`read_instructions(topic="curation.propose")` shows worked plans), and submit it with `manage_memory(operation="curation.propose", arguments={"plan": ...})`. To move many pages into one project or rename them to their titles, `manage_memory(operation="curation.relocate", arguments={"pages": [...], "to_project": ...})` derives the plan instead.
6. Show the preview from `inspect_memory(operation="curation.preview", arguments={"run_id": ...})`: its actions, blockers and fingerprint. Proposing and previewing are `structural_suggestions`; running the plan is `restructure_execution`, so get one explicit confirmation for that exact fingerprint. A request to inspect or improve notes is not that confirmation.
7. Run it with `manage_memory(operation="curation.apply", arguments={"run_id": ..., "plan_id": ..., "expected_plan_fingerprint": ..., "why": ...})`. While the reply's `next_action` is `resume`, continue with `manage_memory(operation="curation.resume", arguments={...})` without asking again; on `partial` or `blocked` without `resume`, stop and report.
8. To undo, derive a new plan with `manage_memory(operation="curation.propose_compensation", arguments={...})`, preview it and get a fresh confirmation before `manage_memory(operation="curation.apply_compensation", arguments={...})`. An explicit undo request may use `manage_memory(operation="curation.revert", arguments={...})`.
9. Promote a relation label that recurs unregistered (`inspect_memory(operation="maintenance.audit", arguments={"categories": ["unregistered_relation"]})`) with `inspect_memory(operation="schema.propose_relation", arguments={...})`, then `manage_memory(operation="schema.save_relations", arguments={...})`.

## Output contract
What changed, the plan fingerprint that was confirmed, and anything left blocked or proposed.

## Save rules
Curation changes structure and links. The content of Sources and Evidence never changes; their links move only through a plan's steps.

## Mistakes to avoid
Do not mass-edit without reading the notes. Do not collapse distinct conclusions because they share words. Do not reuse one confirmation for another plan, for compensation, or as standing approval.
