## MODIFIED Requirements

### Requirement: Workflow schema supports standalone and open companion declarations

A workflow contract SHALL contain exactly, in canonical order, `type`, `contract_id`, `schema_version`, `key`, `title`, `lifecycle`, `scope`, `planning`, `companions`, `capture`, and `planning_transition`. `type` SHALL be `workflow-contract`; `schema_version` SHALL be integer `1`; `contract_id` SHALL be lowercase canonical RFC 4122 UUID text; `lifecycle` SHALL be `active` or `archived`. `scope` SHALL contain exactly `projects`, `domains`, and `activities`, each a unique list of zero to 16 sorted tokens. Populated dimensions SHALL be ANDed and values within one dimension SHALL be ORed. `planning` SHALL contain exactly `mode`, whose value is `standalone` or `companion`. `companions` SHALL be empty in standalone mode and SHALL contain one to eight exact `{key, name, owns}` mappings sorted by key in companion mode; keys SHALL be unique, each `owns` SHALL contain one to 16 unique sorted ownership tokens, and ownership tokens SHALL be unique across the entire contract. Co-ownership is not represented in v1. `capture` SHALL contain exactly `durable_intent` and `observed_outcomes`, each `explicit` or `proactive`; proactive SHALL remain capped by active prominence. `planning_transition` SHALL be `explicit-only` or `propose-after-outcome`.

Project selectors SHALL reuse the canonical project-key grammar `^[a-z][a-z0-9-]{0,40}$`. Contract keys, companion keys, domain selectors, and activity selectors SHALL be 1–64 ASCII bytes matching `^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`. Ownership tokens SHALL be 3–128 ASCII bytes matching `^[a-z][a-z0-9_-]{0,31}(?:\.[a-z][a-z0-9_-]{0,31})+$`. Titles and companion names SHALL be NFKC-normalized, trimmed, control-free strings of 1–128 UTF-8 bytes. A file SHALL be at most 64 KiB. An exact inventory scan SHALL stop at 512 files or 8 MiB and refuse `WORKFLOW_CONTRACT_SCAN_LIMIT` without a total or partial candidate set if either bound is exceeded. A completed scan SHALL return at most 128 released summaries plus the exact released total and a projection-truncation flag. Explanations SHALL be at most 4 KiB; findings and ambiguity candidates SHALL be capped at 32 and 16. Unknown, missing, unsorted, duplicate, unnormalized, or over-limit values SHALL be invalid rather than silently repaired during reads.

Canonical writing SHALL use UTF-8 LF text, YAML `---` delimiters, the field order above, two-space indentation, and the package's pinned safe-dump settings `sort_keys=False`, `allow_unicode=True`, `default_flow_style=False`, and `width=4096`. Semantic fingerprint SHALL be SHA-256 over compact sorted-key JSON of the normalized semantic mapping and SHALL exclude path, filename, authored body, generated presentation, and exact source bytes; source hash SHALL separately cover exact file bytes. Tool keys and ownership tokens SHALL remain open vocabularies. A declaration SHALL NOT assert that the companion is installed, authenticated, reachable, or capable of any operation.

Activity selectors SHALL also be keys of the activity registry (task-guidance). A contract save SHALL refuse an activity selector that is not a current canonical key in the caller's view of that registry; for an alias, or for a deprecated key with a replacement, the refusal SHALL name the canonical key, and every refusal SHALL name `schema_memory(subject="activities")`. A read of a stored contract whose activity selector is not a current canonical key SHALL report the bounded warning `unregistered_activity` with that selector, SHALL keep the contract valid and SHALL keep resolving it, so that a contract saved before its activity was registered does not make the inventory invalid.

#### Scenario: Standalone is complete without an integration
- **WHEN** a valid standalone contract resolves for a life, creative, research, or software scope
- **THEN** Exomem Planning may hold the complete durable outcome-to-work-item hierarchy and no companion is required

#### Scenario: Any companion can declare bounded ownership
- **WHEN** a user authors a companion with a syntactically valid new tool key and namespaced ownership tokens
- **THEN** validation accepts the declaration without a tool-specific adapter and resolution returns the tokens as authored data

#### Scenario: Companion declaration is not capability discovery
- **WHEN** a contract names a tool unavailable on the active client
- **THEN** the resolved decision reports the declared companion separately from active capabilities and never claims the tool can be called

#### Scenario: Conflicting companion ownership refuses
- **WHEN** two companions in one contract both list `software.requirements`
- **THEN** validation refuses the contract because v1 requires one declared owner per ownership token

#### Scenario: A save refuses an unregistered activity selector
- **WHEN** a user saves a contract whose `scope.activities` holds `review-contract` and the activity registry has no such key
- **THEN** the save is refused, and the refusal names `schema_memory(subject="activities")` as the route to register the key

#### Scenario: A stored contract with an unregistered activity keeps resolving
- **WHEN** a contract saved before the activity registry existed holds the activity selector `review-contract`, which the registry does not hold
- **THEN** resolution still considers the contract, and the result carries the warning `unregistered_activity` for that selector
