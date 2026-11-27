---
name: evermatter-research
description: 'Research a focused question with Evermatter: check what is already known, gather and keep sources, and save attributed findings. Use when the user asks to research, investigate, compare options, check a claim, or dig deeper into a topic.'
metadata:
  skill_contract: a011007018185261ff0699edbdd7ffc140db0e5a461dcf160e0e9075f5929c1f
  version: "0.2.0"
---

# evermatter-research

This workflow builds on the `evermatter` skill. Its rules apply here and are not
repeated. If that skill is not loaded, load it; a client that cannot load it calls
`read_instructions(profile="compact")` for the portable rules. Before the first
compiled-note write of the session, read [the authoring contract](references/semantic-authoring.md);
the same file sits in the `evermatter` skill's folder, and `read_instructions(section="authoring")` serves it too.
Each call's fields are in the tool's description and in `read_instructions(topic=<tool or operation>)`.

## Purpose
Answer a focused question and save what should compound.

## When to use
The user asks to research a topic, compare options, investigate a claim or save findings from a research pass.

## Workflow
1. Start from memory: `recall_context(turn=<the user's message verbatim>)` if this conversation holds no context for the subject, then `search_memory(query=...)` for prior conclusions and sources. Check the `skills` that `recall_context` returned for an owner skill that covers this task. If one does, read it with `read_page` and follow its method through the steps below: it decides what to look for and where it goes.
2. Gather external sources only as needed. Keep each one that a finding rests on with `save_source` (`files` for a document, `content` for text). Take a document's title, authors, year, venue and DOI from the document itself; compare them with the reply's `identity` block and fix a mismatch with `options={"correction": ...}`; a document that cites the work is not the work. Set `document_form`; label an abstract or citation record as such.
3. Attribute each finding to its source and keep evidence apart from interpretation.
4. Write the result with `create_note` as a research note unless another type fits better, with every kept source in `sources` and the related notes you found linked.

## Output contract
Findings, cited sources, limits, the saved note and follow-up questions.

## Save rules
Save findings that answer the question, change a decision or create reusable context. Keep attribution explicit.

## Mistakes to avoid
Do not research from scratch before checking memory. Do not save a source list without synthesis. Do not hide uncertainty or gaps in the sources.
