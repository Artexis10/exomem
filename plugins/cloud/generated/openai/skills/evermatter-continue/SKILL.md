---
name: evermatter-continue
description: 'Resume earlier work from Evermatter: what was being done, what was decided and what comes next. Use when the user says continue, pick this back up, where were we, or asks what they were doing on a project or topic.'
metadata:
  skill_contract: d7a2aca80333d9d0b0b4e4e66bfd8b623308e0b63452bd96b5c25a8ad1ed037e
  version: "0.2.0"
---

# evermatter-continue

This workflow builds on the `evermatter` skill. Its rules apply here and are not
repeated. If that skill is not loaded, load it; a client that cannot load it calls
`read_instructions(profile="compact")` for the portable rules. Before the first
compiled-note write of the session, read [the authoring contract](references/semantic-authoring.md);
the same file sits in the `evermatter` skill's folder, and `read_instructions(section="authoring")` serves it too.
Each call's fields are in the tool's description and in `read_instructions(topic=<tool or operation>)`.

## Purpose
Resume prior context from memory, not from guesses about the chat history.

## When to use
The user asks to continue, resume or pick up a project, to recover prior state, or what they were doing on a topic.

## Workflow
1. Call `recall_context(turn=<the user's message verbatim>)` unless this conversation already holds context for the subject. Follow its `next_action`. Check the `skills` that `recall_context` returned for an owner skill that covers this task. If one does, read it with `read_page` and follow its method through the steps below: it decides what to look at and where it goes.
2. For a subject the packet does not cover, call `search_memory(query=..., detail="compact")`; use `search_memory(query=..., deep=true)` when several pages matter and the state needs synthesis.
3. Read the pages that the packet or the hits point to with `read_page`, only where an excerpt is not enough.
4. When the packet names an episode key (`ep-...`), read it with `inspect_memory(operation="episodes.inspect", arguments={"episode": "<ep- key>"})`; an episode is what the previous session decided and left open. Without a key, `search_memory` finds recorded recaps among the Sources; no other episode operation looks one up.
5. Read the open plan items with `query_plan_items()` and then `query_plan_items(collection=...)`, and observed outcomes with `query_records(collection=...)`, when the topic has them. All of these only read.
6. Summarise the current state, the decisions already made, the blockers and the likely next actions.

## Output contract
A short continuation brief that cites the pages it used, with a short list of next actions. Say when the search found little.

## Save rules
Save only what the resumed work newly settles: a decision, a solved problem, a failure, a pattern or a finding. Resuming reads Planning and never moves it; only the user's explicit intent changes a plan item's status.

## Mistakes to avoid
Do not invent prior context. Do not treat a miss in one search as proof of absence. Do not paste long note bodies when a state brief is enough.
