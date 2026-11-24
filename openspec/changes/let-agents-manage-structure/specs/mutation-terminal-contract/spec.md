## ADDED Requirements

### Requirement: A row write names the row and the collection in plain words
For a committed or replayed Records or Planning row write, `user_summary` SHALL name the row by its label and the collection by its title, and SHALL carry no vault path. The row label SHALL be the row's title field when the manifest declares one, else its natural-key values. The sentence SHALL hold for a file-backed and a SQLite-backed write alike.

#### Scenario: A row append names the row and the collection
- **WHEN** an agent appends a row keyed "Meridol Petite" and "2026-11-20" to a store-backed collection titled "Grinders"
- **THEN** `user_summary` names "Meridol Petite, 2026-11-20" and "Grinders", ends with the store name, and contains no path

### Requirement: A row label is released only under the title check
The label SHALL be read from the row the request carries and SHALL appear only when the Records full-release check releases every file that the write touched, the same floor that releases a page title; otherwise the summary SHALL name the collection alone. A write whose request carries no row, such as an update or a retraction, SHALL name the collection and say "an entry".

#### Scenario: A withheld key names the collection alone
- **WHEN** the release check withholds a file that the write touched
- **THEN** `user_summary` names the collection by its title and names no row

### Requirement: A receipt sentence is composed when the reply is projected
The stored terminal keeps ids and hashes, never titles or values, so the sentence SHALL be composed from the receipt and the vault's current state when the reply is projected: a replay of the same request returns the same sentence while the collection's title and the row's title field are unchanged.

#### Scenario: A replay returns the same sentence
- **WHEN** the same row write is replayed and the collection's title and the row's title field are unchanged
- **THEN** `user_summary` is identical to the first reply

### Requirement: A restructure summary states what the user keeps
A `restructure` SHALL carry a `user_summary` that states its effect on what the user keeps when a step adds, retires or restores a field, or merges, splits, retitles, retires or restores a collection. It SHALL name each collection by its title and each field by its label. A restructure of only renames, retypes or rekeys SHALL carry none. The phrasing for each step kind SHALL be defined once, beside the step definitions.

#### Scenario: An added field is stated as what is kept
- **WHEN** a restructure adds a field labelled "price" to a collection titled "Quotes"
- **THEN** `user_summary` says that "Quotes" now keeps the price, and contains no path and none of the structure words

#### Scenario: An internal-only restructure has no summary
- **WHEN** a restructure only rekeys a collection
- **THEN** the reply carries no `user_summary`

### Requirement: A collection's lifecycle write names it by its title
For a committed or replayed create, revise or rebaseline of a Records or Planning collection, `user_summary` SHALL name the collection by its title and SHALL carry no manifest path. The title is the one the committed manifest holds.

#### Scenario: A create names the collection and no path
- **WHEN** an agent creates a collection titled "Grinders"
- **THEN** `user_summary` names "Grinders" and contains no manifest path

### Requirement: Composed summary words name no structure
The words that the server composes in these summaries SHALL NOT name the layer or the structure: Records, Planning, collection, schema, manifest, entity or field. A title the user or the agent chose is not composed text. For these writes, this capability's summary requirements SHALL take precedence over the path wording of "Compact Success Is The Default Projection". The stored terminal and the structured receipt fields SHALL stay unchanged, and the CLI and REST projections MAY keep the path.

#### Scenario: The agent surface carries no path and the CLI may
- **WHEN** a Records row write is projected for an agent over MCP, and again for the CLI
- **THEN** the MCP `user_summary` carries no path and no structure word, and the CLI projection may keep the path

### Requirement: The scaffold and every workflow skill carry the narration rule
A client with the skill installed receives the session profile, which omits the `write` block, so the shipped skill scaffold SHALL carry the narration rule. The scaffold SHALL also say that working on Exomem does not make an unrelated conversation diagnostic. Every standalone workflow skill SHALL embed the same operating rules.

#### Scenario: A workflow skill carries the rule
- **WHEN** the core skill and each standalone workflow skill are read
- **THEN** each contains `NARRATION_RULE` verbatim

#### Scenario: The scaffold keeps unrelated conversation unrelated
- **WHEN** the shipped skill scaffold is read
- **THEN** it says that working on Exomem does not make an unrelated conversation diagnostic

### Requirement: A write lands only in the store the user's connector names
The shipped skill scaffold SHALL tell the agent that the bootstrap's `store` names the Exomem the connector reaches. When the user asks for a different Exomem, the agent SHALL use the connector the user named or say plainly that it is unavailable, and write nothing elsewhere. It SHALL never report a write as landing in a store other than the one `user_summary` names.

#### Scenario: A different Exomem is unavailable, not substituted
- **WHEN** the user asks for a different Exomem and no connector for it is available
- **THEN** the agent says plainly that it is unavailable and writes nothing to the connected store

## MODIFIED Requirements

### Requirement: One Narration Rule Reaches The Agent Through Two Carriers

`mutation_terminal.NARRATION_RULE` SHALL define the narration rule once: tell the user `user_summary`; speak of the user's things in their words, never of collections, Records, Planning, schemas, entities or paths; leave a change with no `user_summary` unmentioned unless asked; mention codes, ids, hashes, retries and sync state only if asked; state a blocker plainly. The compact bootstrap `write.canonical_loop` SHALL carry that sentence as its final step, and so SHALL the shipped skill scaffold.

#### Scenario: Both carriers hold the rule
- **WHEN** the compact bootstrap and the shipped skill scaffold are read
- **THEN** each contains `NARRATION_RULE` verbatim

#### Scenario: Every skill carries the store rule
- **WHEN** the core skill and each standalone workflow skill are read
- **THEN** each tells the agent to use the connector the user named and never to report a write in another store

#### Scenario: The rule keeps internals out of replies
- **WHEN** `NARRATION_RULE` is read
- **THEN** it tells the agent to speak of the user's things in their words, never of collections, Records, Planning, schemas, entities or paths, and to leave a change without a `user_summary` unmentioned unless asked
