## MODIFIED Requirements

### Requirement: Collection manifests may declare and derive claimed domain vocabulary
A collection manifest MAY carry an optional `claims` block with bounded lists of `tags`, `terms`, `entity_types` and `evidence_kinds` (at most 24 entries each), validated with the manifest and round-tripped unchanged through inspection and `describe`. The system SHALL compute a collection's effective claims as the declared claims plus derived claims taken from the collection's own items: observed values of enum fields, observed values of string fields with a bounded number of distinct values, and values of a declared array-of-string tags field. The `claims` block MAY also carry a `match` mapping of frontmatter predicates over the closed keys `type`, `category`, `project` and `tags`, each a list of 1 to 24 non-empty strings, validated with the manifest and documented by `describe`; an unknown key, a non-list or an empty list SHALL refuse naming the key. Accepting `match` SHALL NOT require a manifest schema-version bump, and a manifest without it SHALL parse unchanged. Effective claims SHALL be normalised deterministically, SHALL drop closed-class function words, SHALL be compared through one conservative, idempotent inflection fold applied identically to claims and to observation terms without changing authored storage, SHALL exclude breadth vocabulary, SHALL be maintained as a projection rebuilt by reconcile and folded incrementally by record writes without re-reading the collection, and SHALL be disclosed only for manifests the requesting audience may read. A collection whose effective claims hold fewer than two terms, or whose lifecycle is not active, SHALL NOT be a routing target.

#### Scenario: Declared claims round-trip
- **WHEN** a manifest declares `claims` with tags and terms
- **THEN** validation accepts it, inspection returns the block unchanged, and `describe` documents its shape and bounds

#### Scenario: Oversized or malformed claims refuse
- **WHEN** a `claims` list exceeds 24 entries or carries a non-string entry
- **THEN** manifest validation refuses naming the list and the offending entry

#### Scenario: An existing collection derives claims without editing
- **WHEN** a manifest without `claims` holds items whose enum field takes three values and whose tags field carries recurring tags
- **THEN** after reconcile the collection's effective claims contain those values and tags, and a free-text field with many distinct values contributes nothing

#### Scenario: A record write folds its values into the projection
- **WHEN** a record is appended with an enum value now recurring on two items
- **THEN** the effective claims include the value without the collection being re-read, and a later reconcile produces the same set

#### Scenario: Withheld manifests contribute no claims
- **WHEN** governance withholds a manifest from the requesting audience
- **THEN** no advisory, count or candidate served to that audience names that collection or reveals its claims

#### Scenario: Match predicates validate and describe
- **WHEN** a manifest declares `claims.match` with `type: [failure]` and `project: [example-product]`
- **THEN** validation accepts it, the parsed manifest exposes both predicates, `describe` lists the four match keys, and the existing claim lists are unchanged

#### Scenario: Malformed match predicates refuse
- **WHEN** `claims.match` is a list, names an unknown key, gives a scalar or an empty list, or carries a non-string value
- **THEN** manifest validation refuses with `INVALID_COLLECTION_CLAIMS` naming the offending key

#### Scenario: Function words never become claim terms
- **WHEN** a manifest declares prose terms such as "not because after first use"
- **THEN** none of those words is an effective claim term

#### Scenario: Inflections meet
- **WHEN** claims say `failures` and `dogfood` and a page says `failure` and `dogfooding`
- **THEN** both pairs compare equal, while the authored spellings are stored unchanged
