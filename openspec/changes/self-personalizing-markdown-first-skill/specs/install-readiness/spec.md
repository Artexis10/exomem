## ADDED Requirements

### Requirement: Guided setup generates a starter access policy

After `init`, the `exomem setup` wizard SHALL run an idempotent personalize step that
proposes an access policy for the vault's top-level sibling folders and, on confirmation
(or under `--yes`), writes/merges `Knowledge Base/_access.yaml`. The step SHALL report
`[done]` / `[skipped]` / `[failed]` like every other wizard step and SHALL be safe to
re-run.

#### Scenario: Fresh vault with a sibling folder
- **WHEN** `setup` runs against a vault that has a top-level sibling folder outside
  `Knowledge Base/`
- **THEN** the personalize step proposes and (with `--yes` or confirmation) writes
  `Knowledge Base/_access.yaml`, and the summary shows a `personalize` line

#### Scenario: Re-run converges
- **WHEN** `setup` runs a second time on an already-governed vault
- **THEN** the personalize step reports it skipped because no sibling folders need
  governing

#### Scenario: No sibling folders
- **WHEN** the vault has no folders outside `Knowledge Base/` to govern
- **THEN** the personalize step is skipped without writing a file
