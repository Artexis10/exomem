## ADDED Requirements

### Requirement: Vault-owned activation conventions registry
The product SHALL ship a versioned registry `activation-conventions.yaml` in the skill
scaffold and the Claude Code plugin, loaded by the server, declaring the anchor
membership rules for the `resource` and `hub` anchor kinds (folders, tags and frontmatter
`type` values), the folders the index skips, the ordered state-field and date-field names the current-state resolver
reads, the stopwords the lexical band and derived-name admission ignore, and the
structural resolution threshold `rare_term_max_anchors`. A vault MAY override it at
`<Knowledge Base>/_Schema/activation-conventions.yaml`: adding or dropping anchor
folders, tags, types and state fields, adding (never dropping) stopwords and skip
folders, and replacing the threshold. The shipped registry SHALL reproduce the conventions the compiler used
before this registry existed, so a vault without an override behaves as before. No
server component SHALL write the registry except the governed save this capability
defines, on an agent's explicit request.

#### Scenario: A vault names its own resource folder
- **WHEN** a vault override declares `anchors.resource.add_folders: [Equipment]` and a
  page exists at `Equipment/Field Recorder.md`
- **THEN** the activation index holds that page as a `resource` anchor and a turn naming
  it resolves it

#### Scenario: A vault names its own state field
- **WHEN** a vault override declares `state.prefer_state_fields: [stock]` and a Records
  item for a resolved anchor carries `stock: 3 rolls`
- **THEN** the packet's `current_state` statement for that anchor is drawn from `stock`

#### Scenario: A vault written in another language
- **WHEN** a vault override adds `der`, `die` and `das` to `stopwords`, and a turn and an
  anchor title share `die` and one title word
- **THEN** `die` does not count towards `lexical_overlap`, which the same pair earned
  before the override

#### Scenario: A stopword cannot be removed
- **WHEN** an override declares `stopwords.drop`
- **THEN** the key is ignored with a finding and every shipped stopword still applies

#### Scenario: No override, no change
- **WHEN** a vault has no override
- **THEN** anchor membership, state-field order, date-field order, the stopword set and
  the rare-term threshold equal the values the compiler used before the registry existed

#### Scenario: A denser vault tightens the rare-term threshold
- **WHEN** a vault override declares `resolution.rare_term_max_anchors: 1` and a turn
  shares one name word with two anchors
- **THEN** neither anchor earns `rare_term` from that word, and the index admits no
  derived short name whose words name more than one anchor

#### Scenario: The threshold cannot be set out of range
- **WHEN** an override declares `resolution.rare_term_max_anchors: 0` or a non-integer
- **THEN** the shipped value applies and `generation.conventions_findings` reports the
  rejected value

#### Scenario: The threshold cannot outgrow a small vault
- **WHEN** the index holds 40 anchors and an override declares
  `resolution.rare_term_max_anchors: 10`
- **THEN** the shipped value applies with a finding, because ten exceeds both three and
  one hundredth of the anchors held

#### Scenario: The evidence rules are not configurable
- **WHEN** an override declares any key under `resolution` other than the thresholds the
  shipped registry names
- **THEN** the key is ignored with a finding and resolution behaves as shipped

### Requirement: Membership rules cannot claim reserved or raw-material folders
A folder rule SHALL be a knowledge-base-relative path prefix of at most three segments.
The loader SHALL reject, with a finding and without affecting the other rules, a rule
that is absolute, contains a parent-directory segment, begins a segment with `.` or `_`,
begins with the knowledge-base folder's own name, names the entity folder, names a tree
holding a Planning or Records collection, or falls inside an append-only tree as the
product defines it. Rules SHALL be compared per segment, case-insensitively, under the
resolver's normalisation. A page inside an append-only tree or a governance tree SHALL
never be an anchor, whatever its tags or type, and a page admitted as a Planning or
Records anchor SHALL never also be admitted by a membership rule. Archived trees SHALL
stay walked.

#### Scenario: Raw material cannot be made an anchor
- **WHEN** an override declares `anchors.hub.add_folders: [Sources/Articles]`
- **THEN** the rule is dropped, `generation.conventions_findings` reports it, and no page
  under `Sources/` becomes an anchor

#### Scenario: A structured collection is not claimed twice
- **WHEN** an override declares `anchors.resource.add_folders: [Planning]`
- **THEN** the rule is dropped with a finding and each Planning item remains exactly one
  anchor

#### Scenario: Staged uploads and templates stay out of the catalogue
- **WHEN** a page under the staging tree or the templates folder carries `tags: [hub]`
- **THEN** it is not an anchor

#### Scenario: One bad rule does not void the file
- **WHEN** an override holds one rejected folder rule and one valid one
- **THEN** the valid rule takes effect and only the rejected rule is reported

### Requirement: Bounded, deterministic conventions
Every convention entry SHALL be matched as a normalised literal, never as a regular
expression. The loader SHALL refuse a file larger than 256 KiB before parsing it, SHALL
cap each section (32 folders, 32 tags and 32 types per anchor kind, 32 skip folders, 24
state fields, 12 date fields, 2,000 stopwords, 64 characters per entry), SHALL ignore
entries past a cap, and SHALL report each ignored entry as a finding.

#### Scenario: Entries past a cap are reported, not silently lost
- **WHEN** an override adds 40 resource folders
- **THEN** the first 32 effective folders apply and the remainder are listed in
  `generation.conventions_findings`

### Requirement: Broken override falls back visibly
A conventions override that cannot be read or parsed SHALL leave activation running on
the shipped registry. Every packet SHALL carry `generation.conventions_source`
(`shipped` or `vault`), `generation.conventions_hash` and
`generation.conventions_findings`.

#### Scenario: Invalid YAML
- **WHEN** the override is not valid YAML
- **THEN** activation uses the shipped registry and the packet reports
  `generation.conventions_source = "shipped"` with a finding naming the failure

### Requirement: A conventions edit rebuilds the sidecar
The conventions digest SHALL be stored in the activation sidecar, and a sidecar whose
stored digest differs from the effective one SHALL be wiped and rebuilt exactly as on a
schema-version mismatch, whether or not any vault file changed. The digest SHALL be part
of the packet cache key and of the continuity token's payload, so that no packet built
under the previous conventions is served and a token minted under them reports `stale`.

#### Scenario: Edit then activate
- **WHEN** an owner adds a resource folder to the override and activates the same turn
  again
- **THEN** the packet is built from an index that includes the new folder's pages and
  `generation.conventions_hash` differs from the earlier packet's

#### Scenario: A rule change alone rebuilds stored aliases
- **WHEN** an owner adds a stopword and no vault page changes
- **THEN** the next activation rebuilds the sidecar, and no derived short name admitted
  under the old list survives if the new list rejects it

#### Scenario: A token from other conventions is stale
- **WHEN** a continuity token minted before a conventions edit is presented after it
- **THEN** `generation.continuity` reports `stale` and the token changes nothing

### Requirement: The conventions registry has a governed write path on every tier
`schema_memory` SHALL accept the subject `activation-conventions` to validate a proposed
override and return its findings, diff it against the effective registry, and save a
reviewed proposal under an expected-hash guard with a stated reason. The save SHALL
write only the override file. The hosted gateway SHALL allow these calls while the
generic file tools remain refused for the schema folder.

#### Scenario: A hosted agent configures its vault
- **WHEN** an agent on a hosted tier saves a reviewed override through `schema_memory`
- **THEN** the override is stored, and a direct file write to the schema folder by the
  same agent is still refused

#### Scenario: Findings come back before anything is written
- **WHEN** an agent validates a proposal holding one rejected folder rule
- **THEN** the response lists the finding and nothing is written
