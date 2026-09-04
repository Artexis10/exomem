## MODIFIED Requirements

### Requirement: A frozen verifier runs only under a pinned identity

A model-backed verifier SHALL run only when its opt-in gate has a truthy value
(`EXOMEM_CLAIM_POLARITY_NLI`, default off), its exact repository-pinned upstream
revision is resident, every file in the pin's artifact manifest is present, the
manifest's names and bytes match the pinned sha256 digest, its label map carries
the version the pin names, and that exact artifact/map pair has a green
verification fixture set. The pin registry SHALL be a repository artifact: no
runtime configuration, environment value, vault content, cache ref, or additional
resident revision may add, select, or alter a pin. Files and revisions not named
by the pin SHALL NOT affect the digest or loaded identity.

The gate being unset, a digest mismatch, missing artifact, missing dependency, or
an unverified pair SHALL refuse the verifier for the process. The verifier's
constructor SHALL receive only the exact resident revision whose declared files
matched the pin, with local-only loading forced; a local load failure SHALL refuse
and SHALL NOT retry a repository model name or access the hub. Non-finite model
logits SHALL refuse that output. The verifier's input SHALL be a fixed
classification shape over the two texts it compares — never an assembled prompt,
never vault text in instruction position — and its output SHALL be drawn from the
label map's closed set.

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
- **THEN** no verifier runs under that name, and the ignored value is reported on
  the diagnostic surface

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

## ADDED Requirements

### Requirement: The admitted NLI map states only relations its head measures

The admitted v2 label map SHALL interpret the selected head's declared
`entailment`, `neutral`, and `contradiction` columns in both text directions. It
SHALL emit `contradict` only for symmetric high-confidence contradiction,
`duplicate` only for mutual high-confidence entailment, `refine` for remaining
one-way high-confidence entailment, and `neutral` otherwise. It SHALL NOT equate
NLI neutrality with topical unrelatedness or use an embedding score to manufacture
a stance label.

#### Scenario: Compatible non-entailing claims stay neutral

- **WHEN** two claims express compatible evidence but neither direction meets the
  entailment threshold
- **THEN** the v2 map emits `neutral`, not `refine` or `unrelated`

#### Scenario: Added detail is a refinement

- **WHEN** one direction meets the entailment threshold and the reverse direction
  does not
- **THEN** the v2 map emits `refine`

#### Scenario: Restatement requires mutual entailment

- **WHEN** both directions meet the entailment threshold
- **THEN** the v2 map emits `duplicate`

#### Scenario: One-direction contradiction is insufficient

- **WHEN** only one direction meets the contradiction threshold
- **THEN** the v2 map does not emit `contradict`

### Requirement: Real-model admission has bounded multilingual evidence

The repository pin SHALL name a real fixture set containing English,
same-language non-English, and mixed-language pairs over every admitted label.
The real pinned bytes SHALL pass every fixture through the production loader and
label map in CI. Documentation and diagnostics SHALL identify the fixture set as
bounded admission evidence and MUST NOT convert a model-card language count into
an Exomem-wide multilingual quality guarantee.

#### Scenario: A real fixture misses

- **WHEN** the exact pinned model produces the wrong label for any multilingual
  fixture
- **THEN** admission refuses the entire verifier pair and the real-model CI lane
  fails

#### Scenario: An unverified language reaches the classifier

- **WHEN** a claim pair uses a language or language combination outside the
  fixture set
- **THEN** any produced label remains advisory model output and no product surface
  claims fixture-backed quality for that language
