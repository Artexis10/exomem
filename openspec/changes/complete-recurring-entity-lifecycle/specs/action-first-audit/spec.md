## MODIFIED Requirements

### Requirement: The audit detects recurring unresolved entity identities
The audit SHALL provide an `entity_recurrence` category that deterministically collects evidence for recurring stable identities from two compatible lanes: unresolved body wikilinks and the closed ordinary-text grammar `identity-frames-v1`. Evidence SHALL be grouped by NFKC-normalised, case-folded identity and SHALL count each eligible page and independent Source, session, or otherwise unprovenanced page origin at most once.

The unresolved-wikilink lane SHALL retain its three-distinct-page spread rule and its compatibility reason `unresolved_identity_recurs`. `identity-frames-v1` SHALL accept only `typed-copula`, `typed-label`, `subject-relation`, `identity-relation`, and `body-field` frames using the frozen core-v1 predicate table, active registry `cue_nouns`, and exact title/alias Entity index. Every frame SHALL carry a non-null predicate ID from that table: the four `copula.*` IDs, two `label.*` delimiter IDs, exact form-specific relation IDs, or exact normalized `field.*` label IDs. It SHALL NOT accept a plugin-added frame, arbitrary field name, null or inferred predicate ID, capitalization cue, model, embedding, or statistical NER result.

An ordinary-text identity SHALL qualify only when it occurs on at least three eligible pages across at least three independent origins, carries at least two distinct material facet atoms from at least two origins, and carries a stable cue admitted by that grammar. A candidate span SHALL be the adjacent 1–8-token, 2–128-byte Unicode span bounded by the frame cue and a clause boundary; pronoun-only, all-numeric, stopword-only, URL, email, path, date/time, code, Markdown-link-target, and cue-only spans SHALL be rejected. Repeated mention-only text, repeated copies of one facet atom, and frequency-matched incidental mentions MUST NOT qualify.

A facet atom SHALL bind grammar version, frame type, predicate ID, normalized cue or exact resolved-Entity counterpart, and the clause skeleton with the candidate replaced by `<identity>`. The evidence and signal fingerprints SHALL bind the grammar version, predicate-table digest, registry fingerprint, material facet hashes, and all disconnected qualifying-context hashes. Same-label contexts SHALL form incompatible clusters only when at least two deterministic compatibility-graph components independently meet the full gate and have mutually ancestor-incompatible explicit family cues or disjoint non-empty resolved-Entity anchor sets.

The category SHALL emit at most one finding per identity, carrying bounded deterministic provenance and samples, full recurrence and truncation counts, material facets, role or membership language, co-occurring resolved entities, type cues, resolution evidence, grammar identity, and registry identity. It SHALL add no write-time work, embedding, model call, confidence float, or page mutation.

#### Scenario: A recurring unresolved identity becomes a candidate
- **GIVEN** three distinct eligible pages whose bodies link an identity that exists neither as a page nor as an active Entity title or alias
- **WHEN** the audit sweeps `entity_recurrence`
- **THEN** one finding is produced carrying reason `unresolved_identity_recurs`, the candidate, the three pages sorted, and bounded near-match evidence

#### Scenario: Frequency inside one page is not spread
- **GIVEN** one page repeats an identity five times and only one other independent origin mentions it
- **WHEN** the audit sweeps `entity_recurrence`
- **THEN** zero findings are produced for that identity

#### Scenario: Plain-text mentions are out of scope for this stream
- **GIVEN** an identity is mentioned in many page bodies but no occurrence matches `identity-frames-v1` with the required stable cue and material facets
- **WHEN** the audit sweeps `entity_recurrence`
- **THEN** zero findings are produced for it

#### Scenario: A registry-resolved identity is never a candidate
- **GIVEN** an identity resolves to one active Entity and every qualifying context is connected to that Entity
- **WHEN** the audit sweeps `entity_recurrence`
- **THEN** zero promotion findings are produced for that identity
- **AND** disconnected qualifying contexts would route only to hydration

#### Scenario: Retired and excluded pages are not evidence
- **GIVEN** an identity has two eligible origins and a third occurrence in a superseded, archived, draft, excluded-access, Entity-subtree, code, or frontmatter-only context
- **WHEN** the audit sweeps `entity_recurrence`
- **THEN** zero findings are produced for that identity
- **AND** a finding never anchors on an ineligible page

#### Scenario: Acting on the advice resolves it by state change
- **GIVEN** a firing unresolved-wikilink candidate
- **WHEN** its linked target page is created
- **THEN** that explicit-link finding is not produced on the next sweep
- **AND** deleting the target restores it without a recorded dismissal

#### Scenario: Findings ride the existing review machinery
- **GIVEN** an `entity_recurrence` finding from either evidence lane
- **WHEN** it is delivered
- **THEN** it is a fingerprint-bound, identity-partitioned review item honouring family dispositions, dismissal suppression, and material-change reopen

#### Scenario: Ordinary recurring identity with reusable facets surfaces
- **GIVEN** an unlinked identity occurs across three independent eligible origins through supported v1 frames with at least two distinct material facets and a stable cue
- **WHEN** the audit sweeps `entity_recurrence`
- **THEN** one finding is produced without requiring a wikilink, capitalization, Latin script, or a registered leaf kind for the candidate

#### Scenario: Frequency-matched incidental mentions stay quiet
- **GIVEN** an identity occurs in the same number of pages and origins as a qualifying identity but carries only mention-only or duplicate context and no reusable facet evidence
- **WHEN** the audit sweeps `entity_recurrence`
- **THEN** zero findings are produced for the incidental identity

#### Scenario: Lowercase and non-Latin identities use the same contract
- **GIVEN** lowercase and non-Latin identities each satisfy the same v1 frame, origin, facet, span, and stable-cue gates
- **WHEN** the audit sweeps `entity_recurrence`
- **THEN** each is collected by the same detector and evidence schema

#### Scenario: Existing disconnected Entity becomes hydration evidence
- **GIVEN** an identity resolves to exactly one active Entity and one or more qualifying material contexts remain disconnected from it
- **WHEN** the audit sweeps `entity_recurrence`
- **THEN** one finding is produced with the resolved Entity, the first bounded disconnected-context batch, the remaining count, and batch fingerprint
- **AND** no duplicate-creation decision is made

#### Scenario: Ambiguous resolution never chooses a target
- **GIVEN** an identity resolves to several active Entities or two incompatible components each independently meet the full recurrence gate
- **WHEN** the audit sweeps `entity_recurrence`
- **THEN** one ambiguity finding carries the bounded alternatives
- **AND** the audit chooses no Entity, type, merge, or mutation

#### Scenario: Unsupported grammar cannot grow implicitly
- **GIVEN** text matches no exact `identity-frames-v1` frame or frozen predicate even though a model might recognise a name
- **WHEN** the audit sweeps `entity_recurrence`
- **THEN** that text contributes no ordinary-text evidence
- **AND** changing the grammar, predicate table, or cue-source rules requires a new version and changes the evidence fingerprint

#### Scenario: Copula and label frames have exact predicate identities
- **WHEN** one context matches `typed-copula` with `was` and another matches `typed-label` with an em dash
- **THEN** their evidence carries `copula.was` and `label.em_dash` respectively, with the registry cue bound separately
- **AND** neither uses null, a sentinel invented by the implementation, or an unregistered table entry

#### Scenario: Collection remains read-only and bounded
- **WHEN** the category scans an eligible corpus
- **THEN** it reuses the audit's parsed-page walk and one registry snapshot
- **AND** performs no model, embedding, background, or write operation
- **AND** every candidate sample is bounded while full counts remain reported
