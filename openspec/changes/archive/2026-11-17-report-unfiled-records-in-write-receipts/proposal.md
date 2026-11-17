# Proposal

## Why

An agent wrote a diagnosis note for a failure it had found, and the canonical home for that failure was a Records collection. The agent then told the user the incident was filed, although no Record existed. Two receipt defects made this easy:

- The note's single `project` argument was dropped without a word. The note type stores a `projects` list, and the validator built a sentence saying the argument was ignored but discarded it. With no project on the page, the router sent the note's advisory to the wrong collection at moderate strength, and no filing disposition followed.
- When the router does find the right collection, the write reply still opens with "Saved -> <note>". Nothing in the sentence an agent repeats to the user says the record was not filed.

## What Changes

- A compiled note whose type stores a `projects` list applies a single `project` argument as a one-item list when no list was given. The reply reports every applied or ignored project argument in `warnings`.
- When a note write's stored terminal carries a records disposition that files, appends an occurrence or asks, `user_summary` adds "not filed in <collection title>" before the store name, and `next_action` carries the disposition's call or question.

A Records write whose Markdown view is still being installed keeps its wording. Its `projection_pending` warning stays in `warnings`. Record queries read the store, so the row is already visible.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `command-surface`: project arguments on compiled notes.
- `mutation-terminal-contract`: an unfiled record is named in the outcome sentence.

## Impact

- `note.py` (validation and the two creation callers)
- `mutation_terminal.py` (outcome sentence and `next_action`)
- the receipt documentation in the scaffold

Replies gain words; no field is removed. The routing decision itself is unchanged and stays with the routing owners.
