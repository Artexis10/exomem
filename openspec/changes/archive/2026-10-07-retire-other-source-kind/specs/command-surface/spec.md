## ADDED Requirements

### Requirement: An agent-facing capture names its source kind

Every capture surface an agent can call SHALL refuse a capture that supplies no source kind, or that supplies a kind recording that no kind was chosen (`unclassified`, or the retired `other`), with the code `SOURCE_KIND_REQUIRED`. The refusal SHALL happen before any byte is fetched or written.

The refusal SHALL list the kinds an agent may choose in this vault, each with the number of source pages filed under that kind's folder, most used first and capped at 30. It SHALL report how many choosable kinds exist in total and state what each count measures and where it was read. For a caller other than the owner, a count SHALL include only the pages that caller may see. The refusal SHALL carry one rule: pick the closest existing kind, or name a new slug, which registers on capture.

The refusal SHALL label failed and incomplete count reads. An unknown count SHALL remain unknown, never zero or a complete partial count. Complete counts SHALL sort before unknown counts, with kind names providing deterministic ties. An unavailable count SHALL NOT prevent the caller from choosing a kind.

The system SHALL NOT infer a kind from the content, the title, or any other part of the material. Choosing the kind is the agent's judgement.

#### Scenario: A kindless capture is refused with the known kinds

- **WHEN** an agent captures text without a source kind
- **THEN** the capture is refused with `SOURCE_KIND_REQUIRED`
- **AND** the refusal lists choosable kinds with their source-page counts, most used first
- **AND** no kind that records no choice is listed
- **AND** nothing in the vault changed

#### Scenario: A kind that records no choice is refused

- **WHEN** an agent captures with the kind `other` or `unclassified`
- **THEN** the capture is refused with `SOURCE_KIND_REQUIRED`
- **AND** nothing in the vault changed

#### Scenario: A kindless file capture fetches nothing

- **WHEN** an agent captures attached file handles without a source kind
- **THEN** the capture is refused before any file is fetched

#### Scenario: A new slug follows the rule

- **WHEN** an agent captures with a kind the vault has never seen
- **THEN** the capture succeeds at that kind's projected folder
- **AND** the vault's registry now carries the kind

#### Scenario: A failed count remains unknown

- **WHEN** a source folder cannot be read completely while constructing the refusal
- **THEN** the refusal labels the affected count as unknown and the read as failed or incomplete
- **AND** it retains the choosable kind without reporting zero or a complete partial count

### Requirement: A capture with no agent in the loop is recorded as unclassified

A capture made where no agent can be asked for a kind SHALL record a missing kind as `unclassified` and file the source under that kind's folder. These surfaces are the terminal UI, the hosted web capture box reached through the private command router, the upload form, and a legacy-vault import. The surface adapter SHALL decide this; no caller argument can claim it.

A kind such a surface supplies SHALL resolve as it would for any capture. A supplied `unclassified` or `other` SHALL still be refused, because only an omitted kind means that nobody was asked.

An owner-library principal alone SHALL NOT establish a human capture. The terminal UI adapter SHALL bind its exception separately from the shared programmatic invocation API.

#### Scenario: The terminal UI saves a kindless thought

- **WHEN** a person saves a thought in the terminal UI without choosing a kind
- **THEN** the source is recorded with the kind `unclassified` under its folder

#### Scenario: The hosted capture box saves a kindless memory

- **WHEN** the hosted web capture box posts a title and text through the private command router
- **THEN** the capture succeeds and is recorded as `unclassified`

#### Scenario: A hosted agent is still refused

- **WHEN** a hosted agent profile posts a capture without a kind
- **THEN** the capture is refused with `SOURCE_KIND_REQUIRED`, its remediation, and the known kinds

#### Scenario: A programmatic library caller still names a kind

- **WHEN** a programmatic caller invokes capture through the shared product API without a kind
- **THEN** the capture is refused before effects despite its owner-library principal

#### Scenario: A legacy-vault import is unclassified

- **WHEN** a legacy-vault file is copied in as a source
- **THEN** the copy records the kind `unclassified`

### Requirement: A URL refusal names another way forward

When a kind that requires a URL is captured without one, the refusal SHALL tell the caller to supply the URL or, when it holds something made from the artifact rather than the artifact itself, to name what it holds as the source kind. The refusal SHALL be text guidance and SHALL NOT map one kind to another.

#### Scenario: A transcript without its video's URL

- **WHEN** an agent captures a video transcript as the kind `video` without a URL
- **THEN** the refusal names `source_kind` as the other way forward
- **AND** a capture of the same text under the kind of what it holds succeeds without a URL

### Requirement: Capture reports classification debt in one advisory suggestion

When a capture commits while the vault holds sources with no chosen kind, the successful result SHALL include at most one bounded advisory suggestion, reported through the same advisory-suggestion channel already used for structural advice and distinguished by its own kind value. Sources with no chosen kind are those filed under the `unclassified` folder, the legacy `other` folder, and the legacy-vault import folder.

The suggestion SHALL report a `strength` of `moderate`, a deterministically ordered list of reason codes, the number of such sources, and the folders that hold them. It SHALL NOT report a numeric confidence, score, or probability.

Detection SHALL be deterministic and local. It SHALL reuse the per-folder counts the capture already takes, and it SHALL NOT perform a model call, a network call, or a further scan, and SHALL NOT introduce persistent state. A caller other than the owner SHALL receive no suggestion, because those counts include pages it may not see. The suggestion is advisory: any detection failure, refusal, or absent optional state SHALL leave the committed capture, its location, and its existing result keys unchanged. When no such source exists the key SHALL be absent rather than null or empty.

The suggestion SHALL reach the caller through the committed-mutation response, and the response layer SHALL re-validate it against bounds declared for its own kind rather than forwarding an unvalidated payload. A suggestion whose payload does not satisfy those bounds SHALL be dropped rather than widening the response contract.

#### Scenario: The suggestion reaches the caller through the committed response

- **WHEN** a capture commits in a vault that holds an unclassified source
- **THEN** the caller's committed response carries the suggestion with its kind, strength, reason codes, source count, and folders
- **AND** the response does not carry payload fields belonging to a different advisory kind

#### Scenario: A malformed classification suggestion is dropped, not forwarded

- **WHEN** a classification suggestion whose payload violates the bounds declared for its kind reaches the response layer
- **THEN** the committed response omits the suggestion entirely
- **AND** the capture itself is unaffected

#### Scenario: Legacy and imported sources count as debt

- **WHEN** a capture commits in a vault holding a legacy `Sources/Other/` page or a legacy-vault import
- **THEN** the suggestion counts that page and names its folder

#### Scenario: A vault with no unclassified source stays quiet

- **WHEN** a classified capture commits in a vault where every source has a chosen kind
- **THEN** the result carries no classification suggestion

#### Scenario: Detection failure does not fail the capture

- **WHEN** classification detection raises during an otherwise successful capture
- **THEN** the capture is still committed at its projected location
- **AND** the result reports its normal success outcome with no suggestion key

#### Scenario: An exempt hosted caller receives no owner advisory

- **WHEN** a resolved hosted caller captures a source in a vault that holds classification debt
- **THEN** the capture follows its ordinary authorization rules
- **AND** the result omits the classification-debt advisory unless that caller is the verified owner

## MODIFIED Requirements

### Requirement: Legacy source clients and already-captured sources remain valid

Every source kind the closed vocabulary previously accepted, except `other`, SHALL remain valid and SHALL resolve to the same location it resolved to before, so no capture behaviour silently moves. `other` SHALL remain a deprecated built-in: a page already filed under it stays readable, findable and filterable by that kind, and no new capture writes it.

Callers SHALL be able to supply the source kind under either the existing parameter name or a preferred equivalent name. When both are supplied with different values the system SHALL refuse rather than silently prefer one. When neither is supplied, the capture follows the agent-facing refusal or the no-agent `unclassified` rule.

Already-captured sources SHALL remain valid without modification, whether or not they carry the newer metadata axes. No migration SHALL be required to adopt this change.

Released hosted descriptor bytes SHALL remain unchanged. Their source-capture runtime semantics SHALL migrate through a versioned operating contract and explicit corrective guidance. A historical optional argument or description SHALL NOT authorize a new unclassified or `other` agent capture.

#### Scenario: Every legacy kind routes exactly as before

- **WHEN** a source is captured with each kind the previous closed vocabulary accepted, other than `other`
- **THEN** every capture succeeds
- **AND** each is stored at the location that kind resolved to before

#### Scenario: A legacy other page stays usable

- **WHEN** a vault holds a page filed under `Sources/Other/` with the kind `other`
- **THEN** it can be read, found by recall, and filtered with the kind `other`

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

#### Scenario: A historical hosted caller corrects its arguments

- **WHEN** a historical hosted profile submits a capture permitted by its old optional-kind guidance
- **THEN** runtime refuses before effects and explains the current meaningful-kind requirement
- **AND** a retry with either existing kind argument and a meaningful value succeeds
- **AND** the archived descriptor remains unchanged

### Requirement: A captured source's classification is correctable

The system SHALL provide one operation that changes a captured source's source kind, its subject domain, or both, resolved through the same open-vocabulary rules that govern capture.

The operation SHALL require a stated reason for the correction, following the existing precedent that a reclassifying move names why the judgement changed.

Supplying neither axis SHALL be refused rather than treated as a no-op relocation, so the operation cannot be used as an unmotivated file move. Supplying a kind that records no choice (`unclassified` or `other`) SHALL be refused with `SOURCE_KIND_REQUIRED`, both in the correction and in its preview.

#### Scenario: A fallback capture is corrected to a real kind

- **WHEN** a source stored under `unclassified` or the legacy `other` kind is reclassified to a meaningful kind with a stated reason
- **THEN** the operation succeeds
- **AND** the source's recorded kind is the canonical form of the supplied value
- **AND** the source is no longer located under the folder it was filed under

#### Scenario: A correction cannot file a source as unclassified

- **WHEN** a reclassification supplies the kind `unclassified` or `other`
- **THEN** the operation is refused
- **AND** the source is unchanged

#### Scenario: A domain is corrected without touching the kind

- **WHEN** only a domain is supplied for a source that already carries a meaningful kind
- **THEN** the recorded kind is unchanged
- **AND** the recorded domain is the canonical form of the supplied value

#### Scenario: A correction with no change is refused

- **WHEN** a reclassification supplies neither a kind nor a domain
- **THEN** the operation is refused
- **AND** the source is not moved

#### Scenario: A correction without a reason is refused

- **WHEN** a reclassification supplies a new classification but no reason
- **THEN** the operation is refused naming the missing reason
- **AND** the source is unchanged

## REMOVED Requirements

### Requirement: The fallback kind means low confidence, never missing vocabulary

**Reason**: There is no fallback kind any more. An agent names the kind, and a capture with no agent in the loop is recorded as `unclassified` debt.

**Migration**: Pass `source_kind` on every agent-facing capture. Legacy `other` pages stay readable and are classified with `manage_memory_file(operation="reclassify")`.

### Requirement: Capture may return one advisory source-classification suggestion

**Reason**: The advisory no longer watches for a recurring fallback pattern, because no capture chooses a fallback. It reports classification debt instead.

**Migration**: Read `unclassified_sources` and `folders` on a `source_classification_debt` suggestion instead of `domain` and `fallback_captures`.
