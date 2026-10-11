## MODIFIED Requirements

### Requirement: A frozen verifier runs only under a pinned identity

A model-backed verifier SHALL run only when its opt-in gate has a truthy value
(`EXOMEM_CLAIM_POLARITY_NLI`, default off), its exact repository-pinned upstream
revision, or a derived artifact named by its digest and recipe and built from that
revision, is resident, every file in the pin's artifact manifest is present, the
manifest's names and bytes match the pinned sha256 digest, its label map carries
the version the pin names, and that exact artifact/map pair has a green
verification fixture set. The pin registry SHALL be a repository artifact: no
runtime configuration, environment value, vault content, cache ref, or additional
resident revision may add, select, or alter a pin. Files and revisions not named
by the pin SHALL NOT affect the digest or loaded identity.

The gate being unset, a digest mismatch, missing artifact, missing dependency, or
an unverified pair SHALL refuse the verifier for the process. The verifier's
constructor SHALL receive only the exact resident revision or derived artifact whose
declared files matched the pin, with local-only loading forced; a local load failure SHALL refuse
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

#### Scenario: A locally rebuilt derived artifact labels nothing

- **WHEN** a host rebuilds a pinned derived artifact from the same upstream revision
  and recipe, and the rebuilt bytes do not match the pinned digest
- **THEN** the verifier refuses as a digest mismatch, and no entry carries a label
  from the rebuilt bytes
