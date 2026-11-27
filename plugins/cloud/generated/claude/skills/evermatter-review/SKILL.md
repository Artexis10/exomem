---
name: evermatter-review
description: 'Work through what Evermatter flags for review: due items, stale or contradictory notes, unprocessed sources, relation suggestions, vocabulary and audit findings. Use when the user asks to review the KB, what needs attention, or to drain the backlog.'
metadata:
  skill_contract: d7a2aca80333d9d0b0b4e4e66bfd8b623308e0b63452bd96b5c25a8ad1ed037e
  version: "0.2.0"
---

# evermatter-review

This workflow builds on the `evermatter` skill. Its rules apply here and are not
repeated. If that skill is not loaded, load it; a client that cannot load it calls
`read_instructions(profile="compact")` for the portable rules. Before the first
compiled-note write of the session, read [the authoring contract](references/semantic-authoring.md);
the same file sits in the `evermatter` skill's folder, and `read_instructions(section="authoring")` serves it too.
Each call's fields are in the tool's description and in `read_instructions(topic=<tool or operation>)`.

## Purpose
Resolve review items safely, one decision at a time.

## When to use
The user asks to review memory, what needs attention, or to drain the review backlog.

## Workflow
1. Check the `skills` that `recall_context` returned for an owner skill that covers this task. If one does, read it with `read_page` and follow its method through the steps below: it decides what to review and where it goes.
2. List the due items with `list_review_items()`, or one queue: `unprocessed_sources`, `unlinked`, `relation_suggestions`, `stale`, `contradiction`, `vocabulary`, `imports`, `upkeep`, `plan_progress` or `procedure_approvals`. Read an item's context with `inspect_memory(operation="review.item", arguments={...})` before deciding.
3. Unprocessed source: prepare a note with `inspect_memory(operation="sources.compile", arguments={"sources": [...]})`, then write it with `create_note` and the source in `sources`.
4. Relation suggestion: accept it with `decide_review_item(review_item=..., decision="accept", relation=<option key>)` once the user confirms, or dismiss it.
5. Stale or contradictory note: read it with `read_page` and decide keep, `edit_note`, `supersede_note`, or `decision="competing"` when two views should both stand.
6. Broader health checks: `inspect_memory(operation="maintenance.audit", arguments={"categories": [...]})`.
7. Record each decision with `decide_review_item(review_item=..., decision=..., why=...)`; `why` starts with a reason code (`intentional:`, `false_positive:`, `handled:`, `deferred:`, `too_frequent:`). `snooze` takes `until`.
8. When the user wants to stop hearing about a whole kind of signal, quiet that family with `manage_memory(operation="review.set_family", arguments={"ref": "exomem://review/family/<family>", "action": "quiet", "why": ...})` instead of lowering engagement. A quiet family is silent, not clean.

## Output contract
The decisions made, the writes they led to, and the items left open.

## Save rules
A review decision changes what is shown, not the evidence. Propose risky changes before making them.
Before you promote or correct material that `artifact_role_promotion` or `transient_state_review` flags, follow [artifact role and transient state](references/audit-checks.md#artifact-role-and-transient-state).

## Mistakes to avoid
Do not delete. Do not treat an item in a queue as proof that something is wrong. Do not accept suggested relations or batch risky changes without the user's confirmation.
