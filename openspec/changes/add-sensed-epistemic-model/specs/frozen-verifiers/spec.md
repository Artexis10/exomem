## MODIFIED Requirements

### Requirement: A frozen verifier runs only under a pinned identity

A model-backed verifier or instrument SHALL run only when all of these hold:

- its opt-in gate has a truthy value: `EXOMEM_CLAIM_POLARITY_NLI` (default off) for the audit's stance verifier, or the sensing setting (`EXOMEM_SENSING`, else the config key `sensing`, default off) for a sensing instrument;
- its exact repository-pinned upstream revision is resident;
- every file in the pin's artifact manifest is present;
- the manifest's names and bytes match the pinned sha256 digest;
- its label map carries the version the pin names;
- that exact artifact and label-map pair has a green verification fixture set.

The pin registry SHALL be a repository artifact: no runtime configuration, environment value, vault content, cache ref, or additional resident revision may add, select, or alter a pin. Files and revisions not named by the pin SHALL NOT affect the digest or loaded identity.

An API instrument is the one exception to resident weights, and it SHALL run only as an explicit opt-in placement (per vault for personal vaults, per tenant for Cloud, off by default). Its identity SHALL be provider, model id and version or snapshot date, flagged `unpinned_weights`. It SHALL pass the same fixture set before its readings are consumed.

The gate being unset, a digest mismatch, a missing artifact, a missing dependency, or an unverified pair SHALL refuse the verifier for the process. The verifier's constructor SHALL receive only the exact resident revision whose declared files matched the pin, with local-only loading forced. A local load failure SHALL refuse, and SHALL NOT retry a repository model name or access the hub. Non-finite model logits SHALL refuse that output.

The input SHALL be either a fixed classification shape over the texts it compares, or a versioned template in which vault text appears only inside delimited data slots and never in instruction position. It SHALL never be a prompt assembled from vault text. The output SHALL be logits or probabilities over the label map's closed set plus abstain, never sampled or generated text.

#### Scenario: An unpinned model never labels

- **WHEN** resident artifacts name another model, revision, manifest, or digest
  than the repository pin
- **THEN** the verifier refuses, affected entries carry no label, and the refusal
  is recorded as a degradation — no fallback produces a verifier label

#### Scenario: A label-map change demands re-verification

- **WHEN** the label map's thresholds, label set, column order, or direction rule
  change without a version bump and fixture re-verification
- **THEN** the verifier refuses to run against the stale pair

#### Scenario: Runtime configuration cannot supply a model

- **WHEN** an environment value names a model absent from the repository pin
  registry
- **THEN** no verifier runs under that name, and the ignored value is reported
  on the diagnostic surface

#### Scenario: The admitted bytes are the loaded bytes

- **WHEN** the exact pinned revision whose artifact digest matched cannot be loaded
  locally
- **THEN** the verifier refuses without retrying the repository model name or
  making a hub request

#### Scenario: Extra cache contents do not change identity

- **WHEN** the cache also contains an unlisted file or another revision of the
  pinned model
- **THEN** admission hashes and loads only the exact pinned revision and artifact
  manifest

#### Scenario: A declared artifact is missing

- **WHEN** any file in the pin's artifact manifest is absent or unreadable
- **THEN** the verifier refuses before constructing the model

#### Scenario: Conventional false values keep the opt-in gate off

- **WHEN** the gate is unset, empty, `0`, `false`, `no`, or `off`, ignoring case
  and surrounding whitespace
- **THEN** the verifier remains refused as gate-off

#### Scenario: Invalid numeric output never becomes a label

- **WHEN** either direction's logits contain NaN or infinity
- **THEN** the label map refuses the output and no polarity label is attached

#### Scenario: A generative instrument never returns text

- **WHEN** an admitted generative instrument judges a pair through its template
- **THEN** the vault texts occupy only the template's delimited data slots
- **AND** the recorded output is a probability vector over the closed labels plus abstain, with no generated text stored or returned

#### Scenario: An API instrument is opt-in and labelled unpinned

- **WHEN** a vault has not opted in to an API placement
- **THEN** no API instrument runs for it
- **AND** when it has opted in, every reading names the provider, model id and snapshot date with `unpinned_weights: true`

### Requirement: Verifier output is provenance-marked queue enrichment only

A frozen verifier's or instrument's output SHALL be recorded as a reading in the durable readings ledger. It SHALL be served only as provenance-marked review enrichment or sensed-family evidence that names the method, model digest or API identity, label-map version and reading id.

Readings MAY originate sensed families. A sensed family SHALL NOT suppress, reorder or alter a structural family's items. Instrument output SHALL NOT enter note canon, decisions, retrieval, ranking, policy or any synchronous write path. Instrument output SHALL NOT create, mutate, accept or supersede any page or relation, except through a governed write that the agent authors and that cites the reading id.

#### Scenario: The label stays on the review surface

- **WHEN** a verifier labels a queue entry
- **THEN** no page, relation, ranking position, or write response changes, and
  the entry's label names its producing digest and label-map version

#### Scenario: A sensed family leaves structural families alone

- **WHEN** a sensed family proposes a tension while structural upkeep families also have items
- **THEN** every structural family's items, order and caps are identical to a run with sensing off

#### Scenario: Canon changes only through the agent's cited write

- **WHEN** the agent accepts a sensed relation
- **THEN** it does so through the existing relation writer, whose recorded evidence cites the reading id
- **AND** no instrument or dreamer path writes that relation itself
