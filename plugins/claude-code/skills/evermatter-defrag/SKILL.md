---
name: evermatter-defrag
description: 'Reconcile duplicate, stale or conflicting memory in Evermatter while keeping history: merge, supersede or leave alone. Use when the user asks to defrag, reconcile or deduplicate notes, or asks what is stale or contradictory on a topic.'
metadata:
  skill_contract: 52a1d82b0ca4826a4dae61efd0e938356a63f195a29f508a20c9392385665e4d
  version: "0.2.0"
---

# evermatter-defrag

This workflow builds on the `evermatter` skill. Its rules apply here and are not
repeated. If that skill is not loaded, load it; a client that cannot load it calls
`read_instructions(profile="compact")` for the portable rules. Before the first
compiled-note write of the session, read [the authoring contract](references/semantic-authoring.md);
the same file sits in the `evermatter` skill's folder, and `read_instructions(section="authoring")` serves it too.
Each call's fields are in the tool's description and in `read_instructions(topic=<tool or operation>)`.

## Purpose
Reconcile duplicate, stale or conflicting memory without losing history.

## When to use
The user asks to defrag or reconcile a topic, find what is stale, or resolve notes that disagree.

## Workflow
1. Check the `skills` that `recall_context` returned for an owner skill that covers this task. If one does, read it with `read_page` and follow its method through the steps below: it decides what to keep and where it goes.
2. Find the candidates: `list_review_items(queue="stale")` and `list_review_items(queue="contradiction")`, then `search_memory(query=...)` on the topic, with `deep=true` when the state needs synthesis.
3. Read each candidate with `read_page`, and its history with `inspect_memory(operation="review.evolution", arguments={...})` when it matters.
4. Group the findings into keep, merge, supersede or leave alone.
5. A changed conclusion: `supersede_note`. One outdated claim inside a note: `edit_note(action="supersede_observation", ...)`. A minor correction: `edit_note`.
6. Duplicates to merge: plan a `fold-duplicate` step through the curation flow of the evermatter-curate workflow (`curation.work_item`, `curation.propose`, preview, one confirmation, `curation.apply`). The survivor keeps the merged notes' sources and links. Afterwards, check `list_review_items(queue="unlinked")` for the survivor.
7. Close the review items you resolved with `decide_review_item(review_item=..., decision=..., why=...)`.

## Output contract
What was kept, merged, superseded or left alone, and why.

## Save rules
History stays readable: supersession and folding keep the old pages linked. Sources and Evidence never change.

## Mistakes to avoid
Do not delete memory. Do not treat closeness of meaning as a contradiction by itself. Do not merge notes that answer different questions.
