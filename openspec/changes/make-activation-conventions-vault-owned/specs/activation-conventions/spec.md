## ADDED Requirements

### Requirement: Vault-owned activation conventions registry
The product SHALL ship a versioned registry `activation-conventions.yaml` in the skill
scaffold and the Claude Code plugin, loaded by the server, declaring the anchor
membership rules for the `resource` and `hub` anchor kinds (folders, tags and frontmatter
`type` values), the ordered state-field and date-field names the current-state resolver
reads, the stopwords the lexical band and derived-name admission ignore, and the
structural resolution threshold `rare_term_max_anchors`. A vault MAY override it at
`<Knowledge Base>/_Schema/activation-conventions.yaml`, adding or dropping entries in
every list section and replacing the threshold. The shipped registry SHALL reproduce the conventions the compiler used
before this registry existed, so a vault without an override behaves as before. No
server component SHALL write the registry.

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
- **WHEN** a vault override adds that language's function words to `stopwords`
- **THEN** a turn and an anchor title sharing only those words do not earn
  `lexical_overlap`

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

#### Scenario: The evidence rules are not configurable
- **WHEN** an override declares any key under `resolution` other than the thresholds the
  shipped registry names
- **THEN** the key is ignored with a finding and resolution behaves as shipped

### Requirement: Membership rules cannot claim reserved or raw-material folders
A folder rule SHALL be a knowledge-base-relative path prefix of at most three segments.
The loader SHALL reject, with a finding and without affecting the other rules, a rule
that is absolute, contains a parent-directory segment, begins a segment with `.` or `_`,
names the entity folder, or falls inside an append-only tree as the product defines it.
A page inside an append-only tree SHALL never be an anchor, whatever its tags or type.

#### Scenario: Raw material cannot be made an anchor
- **WHEN** an override declares `anchors.hub.add_folders: [Sources/Articles]`
- **THEN** the rule is dropped, `generation.conventions_findings` reports it, and no page
  under `Sources/` becomes an anchor

#### Scenario: One bad rule does not void the file
- **WHEN** an override holds one rejected folder rule and one valid one
- **THEN** the valid rule takes effect and only the rejected rule is reported

### Requirement: Bounded, deterministic conventions
Every convention entry SHALL be matched as a normalised literal, never as a regular
expression. The loader SHALL cap each section (32 folders, 32 tags and 32 types per
anchor kind, 24 state fields, 12 date fields, 2,000 stopwords, 64 characters per entry),
SHALL ignore entries past a cap, and SHALL report each ignored entry as a finding.

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

### Requirement: A conventions edit never serves a stale packet
The conventions digest SHALL be part of the activation index identity and of the packet
cache key, so that editing the override rebuilds the disposable index on next use and no
packet built under the previous conventions is served afterwards.

#### Scenario: Edit then activate
- **WHEN** an owner adds a resource folder to the override and activates the same turn
  again
- **THEN** the packet is built from an index that includes the new folder's pages and
  `generation.conventions_hash` differs from the earlier packet's
