---
name: evermatter-reflect
description: 'Turn a session or project episode into reusable memory: decisions, failures, patterns, open questions and next actions. Use when the user asks what was learned, to reflect, to wrap up or close an episode, or to extract patterns from a session.'
metadata:
  skill_contract: d65c5a126f69a24ea6da66108e6b338b07e7950d07008ddb61c17a277407bf05
  version: "0.2.0"
---

# evermatter-reflect

This workflow builds on the `evermatter` skill. Its rules apply here and are not
repeated. If that skill is not loaded, load it; a client that cannot load it calls
`read_instructions(profile="compact")` for the portable rules. Before the first
compiled-note write of the session, read [the authoring contract](references/semantic-authoring.md);
the same file sits in the `evermatter` skill's folder, and `read_instructions(section="authoring")` serves it too.
Each call's fields are in the tool's description and in `read_instructions(topic=<tool or operation>)`.

## Purpose
Convert experience into reusable memory.

## When to use
The user asks what was learned, asks to reflect, closes a project episode or wants patterns from a session.

## Workflow
1. Find what memory already holds on the project or topic: the `recall_context` packet, then `search_memory(query=...)`. Check the `skills` that `recall_context` returned for an owner skill that covers this task. If one does, read it with `read_page` and follow its method through the steps below: it decides what to extract and where it goes.
2. Extract decisions, solved problems, diagnosed failures, reusable patterns, held views and unsure reports (attributed, with their uncertainty), open questions, settled values in full, and next actions.
3. Write each durable conclusion as the note type that fits with `create_note`, or change an existing note with `edit_note` or `supersede_note`. Cite what it draws on in `sources` and link the pages step 1 found.
4. Record the episode itself with `save_episode` (what was worked on, decided and left open) when the user asks or the capture disposition allows; reuse the `episode` it returns on later saves in the same conversation.

## Output contract
What was saved, what was deliberately not saved, and the open questions.

## Save rules
Save conclusions, not a transcript, with enough context to reuse them. Prefer one strong note over several weak ones.

## Mistakes to avoid
Do not capture every turn. Do not flatten failures and patterns into generic summaries. Do not erase uncertainty or next actions.
