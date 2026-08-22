# epistemic-graph

## ADDED Requirements

### Requirement: Structural Relation Suggestions From Authored Semantic Units

`suggest_relations` SHALL return deterministic structural candidates derived
from authored semantic units held in the typed graph sidecar, in addition to the
existing wikilink, frontmatter-source, shared-source and embedding-proximity
candidates. The structural methods SHALL be `unit_relation_lift`,
`shared_open_question`, and `shared_resolution_target`. Every structural
candidate SHALL name a page-level target and SHALL carry a relation type, a
method, and evidence. The operation MUST NOT write Markdown, mutate graph edges,
change supersession fields, or change `find` ranking.

#### Scenario: A typed unit relation is lifted to a page-level proposal

- **WHEN** a page carries a semantic unit whose `relations` metadata names a registered relation kind and target, and the page holds no page-level edge of that kind to that target
- **THEN** `suggest_relations` returns a `unit_relation_lift` candidate proposing that authored kind to that target
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: A plain relation bullet is not lifted

- **WHEN** a page expresses a relation as a plain relation bullet inside a block body, which already produces a page-level edge
- **THEN** `suggest_relations` returns no `unit_relation_lift` candidate for that relation

#### Scenario: An already promoted unit relation is not lifted again

- **WHEN** a page carries a typed unit relation and also a page-level edge of the same relation type to the same target
- **THEN** `suggest_relations` returns no `unit_relation_lift` candidate for that relation type and target

#### Scenario: Pages sharing an open question are proposed as related

- **WHEN** two pages each carry a semantic unit that is an open question with the same normalized question text
- **THEN** `suggest_relations` returns a `shared_open_question` candidate from each page to the other

#### Scenario: Pages answering the same target are proposed as related

- **WHEN** two pages each carry a unit-level `answers` or `resolves` edge to the same target
- **THEN** `suggest_relations` returns a `shared_resolution_target` candidate from each page to the other

### Requirement: Question Recall Spans Both Indexed Unit Axes

Structural question matching SHALL select question units from both the unit-kind
axis and the unit-category axis, so that a rich open-question block, a
rich open-question block carrying a category override, and a compact question
observation all participate. The selection SHALL use separately indexed branches
rather than a disjunction across the two columns. Question text SHALL be
normalized identically on both sides of the comparison; that normalization is
ASCII-only case folding plus removal of trailing question marks, and those
limits SHALL be treated as deterministic recall limits rather than defects.

#### Scenario: All three question forms match one another

- **WHEN** one page carries a rich open-question block, a second carries a rich open-question block with a category override, and a third carries a compact question observation, all with the same question text
- **THEN** each page receives a `shared_open_question` candidate naming both of the others

#### Scenario: Case and trailing question marks do not prevent a match

- **WHEN** two pages carry the same ASCII question text differing only in letter case and a trailing question mark
- **THEN** the two pages receive a `shared_open_question` candidate for each other

#### Scenario: A non-ASCII case difference is not normalized away

- **WHEN** two pages carry question text differing only in the case of a non-ASCII letter
- **THEN** no `shared_open_question` candidate is returned for that pair

### Requirement: Structural Suggestions Share One Snapshot And Soft-Fail

All structural generators for one page SHALL read the typed graph sidecar
through a single validated read snapshot. When that snapshot is unavailable,
`suggest_relations` SHALL return zero structural candidates, SHALL still return
its deterministic non-structural candidates, and MUST NOT raise. Each structural
generator SHALL issue a bounded, constant number of sidecar queries independent
of corpus size.

#### Scenario: Unavailable sidecar still yields deterministic suggestions

- **WHEN** the graph read snapshot is unavailable and `suggest_relations` is called for a page carrying wikilinks and typed unit relations
- **THEN** the wikilink candidates are returned, no structural candidate is returned, and no error is raised

#### Scenario: Query count does not grow with the corpus

- **WHEN** `suggest_relations` runs against a two-page corpus and against a corpus of two hundred additional matching pages
- **THEN** the number of sidecar queries issued is the same in both cases

### Requirement: Structural Candidates Aggregate And Are Bounded

Each structural generator SHALL emit at most one candidate per pair of proposed
relation type and target, folding every contributing match into a list inside
that candidate's evidence. Each generator SHALL emit a bounded number of
candidates per page, and each candidate SHALL fold a bounded number of matches
into its evidence. Repeated calls over an unchanged corpus SHALL return
identical candidates.

#### Scenario: Two matches between the same pair yield one candidate

- **WHEN** two pages share two distinct open questions
- **THEN** exactly one `shared_open_question` candidate is returned for that pair, and its evidence lists both questions

#### Scenario: Two authoring units yield one lift candidate

- **WHEN** two semantic units on one page each author the same relation kind to the same target, and no page-level edge of that kind exists
- **THEN** exactly one `unit_relation_lift` candidate is returned, and its evidence lists both authoring units

#### Scenario: A large matching corpus stays bounded

- **WHEN** two hundred pages each carry the same open question as the requested page
- **THEN** at most three `shared_open_question` candidates are returned

### Requirement: Structural Evidence Identifies The Driving Unit

A structural candidate whose evidence is derived from a page other than the
candidate's source page SHALL include that other page's semantic unit identity —
its unit reference, its anchor, and the relation kinds it used where applicable.
A dismissed candidate SHALL therefore resurface with a different fingerprint
when the page that produced its evidence changes, even while the source page
remains byte-identical.

#### Scenario: Dismissal expires when the other page changes

- **WHEN** a `shared_open_question` candidate is dismissed and the other page's question unit is then re-anchored while the source page stays byte-identical
- **THEN** the candidate reappears in the relation queue with a different fingerprint

#### Scenario: Lift evidence names its authoring unit

- **WHEN** a `unit_relation_lift` candidate is returned
- **THEN** its evidence names the authoring unit's unit reference and anchor, the authored relation label, and the resolved relation family

### Requirement: Structural Generators Run After Deterministic And Before Optional Generators

Structural generators SHALL be invoked after the wikilink, frontmatter-source
and shared-source generators and before the optional embedding-proximity
generator, so that the deterministic candidates users already depend on are
never displaced and structural candidates are never ranked behind an optional
lane. This ordering interacts with the response truncation limit deliberately.

#### Scenario: Candidate order places structural candidates between the two groups

- **WHEN** `suggest_relations` returns candidates from every generator for one page
- **THEN** the first structural candidate appears after the first shared-source candidate and before the first embedding-proximity candidate
