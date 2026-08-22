# graph-semantic-integrity

## ADDED Requirements

### Requirement: Structural Co-Participation Suggestions Remain Semantically Neutral

A structural suggestion method that observes only co-participation — two pages
carrying the same open question, or two pages whose units answer or resolve the
same target — SHALL propose the registered symmetric relation `relates_to` and
nothing else. It MUST NOT propose `duplicates`, `refines`, `supports`,
`contradicts`, or any other directional epistemic relation, because a
page-level target cannot express a claim that holds only between two semantic
units.

#### Scenario: Shared open question does not assert duplication

- **WHEN** two pages carry the same normalized open question and no directional relation between the pages is observed
- **THEN** the shared-open-question candidate proposes `relates_to` and identifies the shared question and the other page's question unit in its evidence

#### Scenario: Shared resolution target does not assert refinement

- **WHEN** two pages each carry a unit-level `answers` or `resolves` edge to the same target
- **THEN** the shared-resolution-target candidate proposes `relates_to` and identifies the shared target and both sides' relation kinds in its evidence

### Requirement: A Lifted Relation Kind Must Already Be Authored On The Page

A structural method that proposes a directional epistemic relation SHALL do so
only by promoting a relation kind that is already present on a unit-level
relation edge of that same source page. The proposed relation type SHALL be the
label the author wrote on that unit edge. The method MUST NOT propose a relation
kind absent from the source page's own unit-level edges, and MUST NOT derive a
relation kind from similarity, proximity, or any other measurement.

#### Scenario: Only authored kinds are proposed

- **WHEN** a unit-relation-lift candidate is returned for a page
- **THEN** its proposed relation type appears verbatim as the authored relation label on a unit-level relation edge of that page

#### Scenario: An unauthored kind is never proposed

- **WHEN** a page carries no unit-level relation edge of a given kind
- **THEN** no structural method proposes that kind for that page

### Requirement: No Structural Method Proposes Causality

No structural relation-suggestion method SHALL propose `causes`, `caused_by`, or
any other relation in the causality family, even when the author has written
such a relation on one of the page's semantic units. Promoting a unit's causal
claim to a page-level proposal would assert a mechanism between the pages that
the author did not write.

#### Scenario: An authored causal unit relation is not lifted

- **WHEN** a page carries a unit-level `causes` or `caused_by` relation to a target
- **THEN** no structural candidate proposing a causality-family relation is returned for that page

### Requirement: Structural Lifts Respect Relation Registry Standing

A structural lift SHALL propose only relation kinds that the registry resolves
with standing `core`, `alias`, or `extension`, and whose resolved family is
within the method's declared allowlist. It MUST NOT propose a kind that the
registry reports as unregistered, deprecated, or in violation of its declared
scope, and the family allowlist SHALL be resolved through the registry at call
time so a vault relation extension participates without a code change.

#### Scenario: An unregistered authored label is not lifted

- **WHEN** a page carries a unit-level relation whose label the registry does not resolve
- **THEN** no structural candidate proposing that label is returned

#### Scenario: An out-of-family registered kind is not lifted

- **WHEN** a page carries a unit-level relation whose resolved family is outside the lift allowlist
- **THEN** no structural candidate proposing that kind is returned

### Requirement: Structural Suggestions Remain Proposal-Only

Adding structural suggestion methods SHALL NOT write Markdown, mutate accepted
graph edges, change retrieval ranking, invoke a reasoning model, or require a
sidecar schema change or rebuild. The response SHALL continue to report
`mutated=false` and `model_suggestions_available=false`.

#### Scenario: Structural suggestion call is non-mutating

- **WHEN** `suggest_relations` returns structural candidates for a page
- **THEN** the response reports `mutated=false` and `model_suggestions_available=false`
- **AND** the vault Markdown bytes and the graph sidecar edges are unchanged
