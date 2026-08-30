## ADDED Requirements

### Requirement: A frozen verifier runs only under a pinned identity

A model-backed verifier SHALL run only when its resolved weights match a pinned
sha256 digest, its label map carries the version the pin names, and that
(digest, label-map version) pair has a green verification fixture set. A digest
mismatch, missing weights, missing dependency, or unverified pair SHALL refuse
the verifier for the process. The verifier's input SHALL be a fixed
classification shape over the texts it compares — never an assembled prompt,
never vault text in instruction position — and its output SHALL be drawn from
the label map's closed set.

#### Scenario: An unpinned model never labels

- **WHEN** the configured weights resolve to a digest other than the pinned one
- **THEN** the verifier refuses, the affected entries carry no stance, and the
  refusal is recorded as a degradation — no label is produced by any fallback
  under the verifier's method name

#### Scenario: A label-map change demands re-verification

- **WHEN** the label map's thresholds or label set change without a version
  bump and fixture re-verification
- **THEN** the verifier refuses to run against the stale pair

### Requirement: Verifier output is provenance-marked queue enrichment only

A frozen verifier's labels SHALL attach only to review-queue entries, carrying
the method, model digest, and label-map version that produced them. Verifier
output SHALL NOT enter note canon, decisions, retrieval, ranking, policy, or
any synchronous write path, and SHALL NOT create, mutate, accept, or supersede
any page, relation, or proposal.

#### Scenario: The label stays on the review surface

- **WHEN** a verifier labels a queue entry
- **THEN** no page, relation, ranking position, or write response changes, and
  the entry's label names its producing digest and label-map version

### Requirement: Verifier absence degrades to byte-identical silence

With the verifier's extra uninstalled, its gate off, or its pin unsatisfied,
every product surface SHALL behave byte-identically to a build that has no
verifier tier at all, apart from an explicit degradation record on the
diagnostic surface. A per-entry failure during an enrichment pass SHALL leave
that entry unenriched and recorded, and SHALL NOT abort the pass.

#### Scenario: Uninstalled means invisible

- **WHEN** the extra is not installed and a full audit and write cycle runs
- **THEN** queue entries, write responses, and rankings are byte-identical to
  today's, and only the diagnostic surface names the absent verifier

#### Scenario: One bad entry does not take down the pass

- **WHEN** the model raises on one claim pair mid-pass
- **THEN** that entry is unenriched and recorded as degraded, and every other
  entry's enrichment completes
