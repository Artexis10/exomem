# vault-overview

## ADDED Requirements

### Requirement: Overview hides excluded subtrees

`overview`/`browse_memory` SHALL prune `excluded`-tier subtrees from the reported
folder tree and from every count and coverage figure it emits. An excluded folder
or file SHALL NOT appear in the structure, SHALL NOT be sampled, and SHALL NOT
contribute to any aggregate. The report SHALL NOT emit a residual "hidden N"
marker for excluded content, because that count leaks subtree size.

#### Scenario: Excluded subtree is absent from structure and counts

- **WHEN** `overview` runs on a vault containing an `excluded` subtree
- **THEN** neither the subtree nor its files appear in the tree, and no count,
  frontmatter-coverage figure, or sample includes them

#### Scenario: Non-excluded structure is unchanged

- **WHEN** `overview` runs on a vault with no excluded subtrees
- **THEN** the report is identical to current behavior
