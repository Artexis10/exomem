## Why

On 1 Nov 2026 a connected assistant committed a note after a passing `validate_only` that reported no review was needed. It sent a reviewed-none disposition and reason without a hash, and the commit refused with `relation review fields must be supplied together (missing: [])`. The message named no field, because the review error reaches the command layer with an empty `missing` list that the command layer always prints. The assistant retried and the save succeeded. It then repeated the error code to the user in an unrelated conversation.

Three gaps caused this:

- A page that needs no relation review refuses review fields. The semantic write contract already says such a page commits without a review artifact.
- An absent review hash on an existing-page edit refuses with `LIFECYCLE_TRANSITION_REVIEW_MISMATCH`, which says nothing about the missing field.
- A successful write returns codes, ids and hashes but no sentence the assistant can pass to the user, and no rule says to pass that sentence rather than the diagnostics.

## What Changes

- One review-field check, shared by validation and commit. A page that needs no review drops supplied review fields and returns one warning. A page that needs a review refuses a commit with missing fields and names each one in `missing`.
- Validation reports the review fields a commit would find missing or ignored, under `relation_review_fields`. It never compares the hash.
- An existing-page edit commit with no hash refuses with `INVALID_RELATION_REVIEW`. A wrong hash keeps `LIFECYCLE_TRANSITION_REVIEW_MISMATCH`.
- Every compact success and replay leads with `user_summary`, built from the stored terminal. The verb comes from a closed `outcome_verb` set on the command.
- One narration rule, `mutation_terminal.NARRATION_RULE`, tells the assistant to pass `user_summary` to the user. The bootstrap `canonical_loop` and the shipped skill both carry it.

## Capabilities

### Modified Capabilities

- `semantic-write-contract`: review fields on a page that needs no review are dropped with a warning; missing fields are named; validation reports both.
- `existing-edit-review-handshake`: an absent hash has its own refusal.
- `mutation-terminal-contract`: compact success leads with `user_summary`; one narration rule reaches the agent through two carriers.

## Impact

- Code: `relation_review.py`, `semantic_writes.py`, `note.py`, `create_file.py`, `link.py`, `replace.py`, `edit.py`, `curation.py`, `commands.py`, `cli_ops.py`, `mutation_terminal.py`, `command_surface.py`, `writer_lease.py`, and the skill scaffold.
- Behaviour: a client that sent review fields for a page that needs none was refused and now succeeds with a warning. Existing result keys keep their names and values; `user_summary` is a new first key.
- Undo: revert the change. No stored data changes.
