## ADDED Requirements

### Requirement: Source kind is an open, extensible vocabulary

Source capture SHALL accept any source kind that normalizes to a safe canonical key, whether or not the product ships that key as a built-in. Acceptance SHALL NOT depend on a code release, a schema migration, or a prior administrative registration step.

The system SHALL normalize a supplied kind to one canonical key, resolve a registered alias to its canonical key, and accept an unregistered but valid key by canonicalizing it. An unregistered key SHALL be recorded in the vault's source-taxonomy registry as part of the same atomic write that captures the source, so a later capture of the same kind resolves as registered.

The system SHALL refuse a supplied kind only when it cannot be normalized into a safe canonical key, or when it is a near-miss of an already-known key. A near-miss refusal SHALL name the existing key it resembles so the caller can correct itself, and SHALL state how a deliberately similar new key can still be introduced.

Built-in kinds SHALL be generic and free of any user-specific identifier.

#### Scenario: A previously unseen meaningful kind is accepted

- **WHEN** a source is captured with a source kind the product has never seen, which normalizes to a valid canonical key
- **THEN** the capture succeeds
- **AND** the stored kind is the canonical form of the supplied value, not a fallback
- **AND** no code change, schema migration, or separate registration call was required
- **AND** the vault's source-taxonomy registry now carries that key

#### Scenario: A registered alias resolves to its canonical key

- **WHEN** a source is captured with a value registered as an alias of a canonical kind
- **THEN** the stored kind is the canonical key
- **AND** the destination is the one the canonical key projects to

#### Scenario: A near-miss of a known kind is refused with its correction

- **WHEN** a source is captured with a kind that differs from an already-known kind only by a small number of characters
- **THEN** the capture is refused
- **AND** the refusal names the known key it resembles
- **AND** the refusal states how a deliberately distinct new key can be introduced

### Requirement: Subject domain is an independent open vocabulary

Source capture SHALL accept an optional subject domain describing what the artifact is about, resolved through the same open-vocabulary rules as source kind and held on an axis independent of it. Any combination of a valid kind and a valid domain SHALL be permitted; neither axis SHALL constrain the values allowed on the other.

Domain SHALL be optional. A capture that supplies no domain SHALL succeed and SHALL be stored without one.

Built-in domains SHALL be generic and free of any user-specific identifier.

#### Scenario: A previously unseen meaningful domain is accepted

- **WHEN** a source is captured with a subject domain the product has never seen, which normalizes to a valid canonical key
- **THEN** the capture succeeds
- **AND** the stored domain is the canonical form of the supplied value
- **AND** no code change or schema migration was required

#### Scenario: Kind and domain vary independently

- **WHEN** sources are captured pairing the same kind with different domains, and the same domain with different kinds
- **THEN** every combination succeeds
- **AND** each stored source carries exactly the kind and domain supplied for it

#### Scenario: Domain may be omitted

- **WHEN** a source is captured with a kind but no domain
- **THEN** the capture succeeds
- **AND** the stored source declares no domain

### Requirement: Project association is separate, multi-valued, and never a storage constraint

Source capture SHALL accept zero or more project keys for one source, resolved through the existing project-key rules. Project association SHALL be independent of both source kind and subject domain, and SHALL NOT influence where the source is stored.

A single source SHALL be able to serve more than one project simultaneously without duplication. Provenance SHALL remain carried by the existing stable identity and reference fields rather than by project membership.

#### Scenario: One source serves several projects

- **WHEN** a source is captured naming more than one project key
- **THEN** the capture succeeds as a single source with a single stable identity
- **AND** every named project is recorded on it
- **AND** its location is unchanged from the same capture with no project named

#### Scenario: Project keys do not appear in the source location

- **WHEN** two sources sharing a kind and a domain are captured with different project keys
- **THEN** both are stored in the same location
- **AND** neither location contains a project key

### Requirement: The source location is a deterministic projection of canonical semantic metadata

The system SHALL derive a captured source's location from its canonical kind and, when present, its canonical domain. The semantic metadata SHALL be authoritative and the location SHALL be a projection of it; the directory structure SHALL NOT be the classification model.

The projection SHALL distinguish a canonical machine key from its human-facing path segment, so a canonical key and the segment it projects to may differ. A path segment SHALL be derived only from an already-validated canonical key, never from raw caller input. When the registry declares no path segment for a key, the system SHALL derive one deterministically from that canonical key.

The projection SHALL omit the domain level when no domain is present, and SHALL produce at most two levels beneath the source root. The same canonical metadata SHALL always project to the same location.

The system SHALL NOT require or enforce agreement between an already-captured source's location and its recorded metadata.

#### Scenario: A research report about travel is not stored under the fallback

- **WHEN** a source is captured with a research-report kind and a travel domain
- **THEN** the capture succeeds
- **AND** its location is not the fallback location or any descendant of it
- **AND** its location reflects both the kind and the domain

#### Scenario: A canonical key and its path segment may differ

- **WHEN** a kind whose registered path segment differs from its canonical key is captured
- **THEN** the stored metadata carries the canonical key
- **AND** the location carries the registered path segment

#### Scenario: An omitted domain omits a level

- **WHEN** the same kind is captured once with a domain and once without
- **THEN** the capture with a domain is stored one level deeper than the capture without
- **AND** neither location exceeds two levels beneath the source root

#### Scenario: Projection is deterministic

- **WHEN** two sources are captured with identical canonical kind and domain
- **THEN** both resolve to the same directory

#### Scenario: An already-captured source is not required to match the projection

- **WHEN** a source exists whose location does not match what its recorded metadata would project to today
- **THEN** it remains valid, readable, and retrievable
- **AND** no error, finding, or warning is raised about the mismatch
- **AND** it is not moved

### Requirement: Open vocabulary does not weaken filesystem safety

The system SHALL reject or normalize any supplied kind or domain that could otherwise influence the stored location as path input. Path traversal segments, absolute paths, drive-qualified paths, network share paths, embedded path separators, bare or repeated dot segments, trailing dots and spaces, control characters, and values that normalize to nothing SHALL NOT reach a path segment.

The system SHALL refuse a canonical key that would project to a filesystem-reserved device name, and SHALL refuse to register a new key whose path segment would collide case-insensitively with that of a different existing key.

The system SHALL bound the length of a canonical key.

#### Scenario: Traversal and absolute path forms never become a path segment

- **WHEN** a capture supplies a kind or domain containing traversal segments, a leading separator, a drive qualifier, a network share prefix, an embedded separator in either direction, a bare dot, or a repeated dot
- **THEN** the value is either refused or normalized to a safe canonical key
- **AND** the resulting location remains beneath the source root
- **AND** no supplied separator or dot segment appears as a path segment

#### Scenario: Reserved device names are refused

- **WHEN** a capture supplies a kind or domain whose canonical key names a filesystem-reserved device
- **THEN** the capture is refused with a remediation message

#### Scenario: Colliding path segments are refused

- **WHEN** registering a new canonical key would produce a path segment differing from an existing key's segment only by letter case
- **THEN** the registration is refused

#### Scenario: Degenerate and oversized values are refused

- **WHEN** a capture supplies a kind or domain that is empty, consists only of characters that normalize away, or exceeds the canonical key length bound
- **THEN** the capture is refused rather than stored under a fallback

### Requirement: The fallback kind means low confidence, never missing vocabulary

The system SHALL treat the `other` kind as a low-confidence classification. A supplied kind that resolves to a safe canonical key SHALL NOT be recorded or routed as `other` on the grounds that the key was not previously known.

A capture that supplies no kind SHALL remain permitted and SHALL resolve to `other`, so classification is never a precondition for preserving material.

No capture surface SHALL publish the fallback as its default classification argument. Every surface through which a source can be captured SHALL be able to express kind, domain, and project association; a surface that can only express the fallback reproduces the defect this capability removes.

#### Scenario: No capture surface defaults to the fallback

- **WHEN** a source is captured through any supported capture surface without a kind argument
- **THEN** no fallback value was supplied on the caller's behalf by that surface's defaults
- **AND** that surface accepts a kind, a domain, and project keys when the caller has them

#### Scenario: A confidently supplied unknown kind is never demoted

- **WHEN** a source is captured with a meaningful kind the product has never seen
- **THEN** the stored kind is that kind
- **AND** neither the stored kind nor the location is the fallback

#### Scenario: An unclassified capture still succeeds

- **WHEN** a source is captured with no kind supplied
- **THEN** the capture succeeds and resolves to the fallback kind
- **AND** no classification argument was required

### Requirement: Capture may return one advisory source-classification suggestion

When a capture resolves to the fallback kind while carrying evidence that a real kind exists, the successful result SHALL include at most one bounded advisory suggestion, reported through the same advisory-suggestion channel already used for structural advice and distinguished by its own kind value.

The suggestion SHALL report a `strength` of exactly `strong` or `moderate` and a deterministically ordered list of reason codes. It SHALL NOT report a numeric confidence, score, or probability.

Detection SHALL be deterministic and local. It SHALL NOT perform a model call, a network call, or a whole-corpus scan, and it SHALL NOT introduce persistent state. The suggestion is advisory: any detection failure, refusal, or absent optional state SHALL leave the committed capture, its location, and its existing result keys unchanged. When no condition is detected the key SHALL be absent rather than null or empty.

Material that carries the fallback kind as an internal marker rather than as a user classification SHALL NOT be analysed and SHALL NOT produce a suggestion.

The suggestion SHALL reach the caller through the committed-mutation response, and the response layer SHALL re-validate it against bounds declared for its own kind rather than forwarding an unvalidated payload. A suggestion whose payload does not satisfy those bounds SHALL be dropped rather than widening the response contract.

#### Scenario: The suggestion reaches the caller through the committed response

- **WHEN** a capture that produces a classification suggestion commits
- **THEN** the caller's committed response carries that suggestion with its kind, strength, reason codes, domain, and fallback count
- **AND** the response does not carry payload fields belonging to a different advisory kind

#### Scenario: A malformed classification suggestion is dropped, not forwarded

- **WHEN** a classification suggestion whose payload violates the bounds declared for its kind reaches the response layer
- **THEN** the committed response omits the suggestion entirely
- **AND** the capture itself is unaffected

#### Scenario: A recurring fallback pattern in one domain suggests a real kind

- **WHEN** several sources have been captured with the fallback kind and the same domain, and a further such capture commits
- **THEN** the capture succeeds unchanged
- **AND** the result carries one advisory classification suggestion of `strong` strength
- **AND** the suggestion names its reason codes in deterministic order
- **AND** the suggestion reports no numeric confidence

#### Scenario: A single unusual fallback capture stays quiet

- **WHEN** one source is captured with the fallback kind and no domain, and no comparable prior capture exists
- **THEN** the result carries no classification suggestion

#### Scenario: A coherently classified capture stays quiet

- **WHEN** a source is captured with a meaningful kind
- **THEN** the result carries no classification suggestion

#### Scenario: Detection failure does not fail the capture

- **WHEN** classification detection raises during an otherwise successful capture
- **THEN** the capture is still committed at its projected location
- **AND** the result reports its normal success outcome with no suggestion key

#### Scenario: Internal fallback markers are never analysed

- **WHEN** material that uses the fallback kind as an internal marker rather than a user classification is written
- **THEN** no classification suggestion is produced for it

### Requirement: Legacy source clients and already-captured sources remain valid

Every source kind the closed vocabulary previously accepted SHALL remain valid and SHALL resolve to the same location it resolved to before this change, so no existing client requires modification and no capture behaviour silently moves.

Callers SHALL be able to supply the source kind under either the existing parameter name or a preferred equivalent name. When both are supplied with different values the system SHALL refuse rather than silently prefer one. When neither is supplied the fallback kind SHALL apply.

Already-captured sources SHALL remain valid without modification, whether or not they carry the newer metadata axes. No migration SHALL be required to adopt this change.

#### Scenario: Every legacy kind routes exactly as before

- **WHEN** a source is captured with each kind the previous closed vocabulary accepted
- **THEN** every capture succeeds
- **AND** each is stored at the location that kind resolved to before this change

#### Scenario: Either parameter name is accepted

- **WHEN** a source is captured supplying the kind under the existing parameter name, and again under the preferred equivalent name
- **THEN** both captures succeed identically

#### Scenario: A conflicting pair of names is refused

- **WHEN** a source is captured supplying both parameter names with different values
- **THEN** the capture is refused naming the conflict

#### Scenario: Existing sources need no migration

- **WHEN** a vault containing sources captured under the previous vocabulary is read, indexed, and searched after this change
- **THEN** every existing source remains valid and retrievable at its original location
- **AND** no migration step was required

### Requirement: Human browsing renders new categories without per-kind code

The generated source index SHALL present every populated category, including one produced by a kind the product does not ship, without requiring a code change for that kind. A category with no registered description SHALL receive a generic one rather than being omitted or breaking the index.

Counts SHALL include sources stored beneath a domain level.

#### Scenario: An unshipped kind appears in the index

- **WHEN** a source is captured with a kind the product does not ship as a built-in
- **THEN** the generated source index lists that category
- **AND** its description is either the registered one or a generic fallback

#### Scenario: Nested sources are counted under their kind

- **WHEN** sources are captured for the same kind with and without a domain
- **THEN** the index count for that kind includes both

### Requirement: Retrieval filters kind, domain, and project independently

Retrieval SHALL support filtering by source kind, by subject domain, and by project key independently of one another, and by any combination of them. These SHALL be addressable as first-class filter fields, and SHALL NOT require the caller to encode classification as tags.

#### Scenario: Each axis filters on its own

- **WHEN** a corpus contains sources spanning several kinds, domains, and projects, and a filter names exactly one axis
- **THEN** the results are exactly the sources matching that axis value
- **AND** no other axis restricts the result

#### Scenario: Axes combine

- **WHEN** a filter names a kind and a domain, or a kind and a project, or all three
- **THEN** the results are exactly the sources matching every named axis value

#### Scenario: Classification is filterable without tags

- **WHEN** sources are captured with kind, domain, and projects but no tags
- **THEN** each axis remains filterable
