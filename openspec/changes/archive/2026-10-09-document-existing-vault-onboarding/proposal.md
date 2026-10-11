# document-existing-vault-onboarding

## Why

The docs assume a fresh `Knowledge Base/`; a first-time user whose vault is
100% pre-existing content (a daily time-log) had no way to learn what exomem
would and wouldn't do with their notes, whether to init into that vault or a
separate one, or how an agent should assess such a vault — the gap that
produced a 168-file brute-force read in the field. `add-vault-overview` and
`add-setup-wizard` shipped the mechanics; this change ships the narrative.

## What Changes

- SETUP-LOCAL.md: new **"Already have a vault full of notes?"** section — the
  write contract in plain words, same-vault vs separate-vault guidance,
  daily-notes vaults specifically (the KB is a compiled layer beside the log,
  never a migration demand), and the worked example: "ask what does this vault
  look like → the agent runs `overview`, one call, not one read per file."
- README quickstart: the existing-vault paragraph links to that section.
- Cross-check: scaffold SKILL.md's "Assessing a vault you didn't build" block
  (shipped in `add-vault-overview`) stays consistent with the new wording.

Docs only — no code, no behavior change, no dependencies.

## Capabilities

### New Capabilities
- `existing-vault-onboarding`: the documented contract for onboarding a vault
  with pre-existing content.

### Modified Capabilities

(none)

## Impact

- `SETUP-LOCAL.md`, `README.md`. No code. `test_scaffold_no_leak.py` and the
  suite stay green (nothing under `src/` changes).
