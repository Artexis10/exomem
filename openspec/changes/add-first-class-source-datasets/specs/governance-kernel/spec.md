## MODIFIED Requirements

### Requirement: Query-time scope membership

Scope membership SHALL be evaluated at query time against an already-parsed Markdown
page or an explicit non-Markdown classification outcome using the scope's selectors
(path globs, projects, tags, types, detector classes, explicit refs) minus its `exclude`
selectors, memoized per immutable content identity and policy fingerprint. Membership
SHALL NOT be materialized as an index-time table and SHALL NOT add a component to the
deletion/upsert fan-out. A policy change SHALL invalidate membership by fingerprint
mismatch.

Non-Markdown membership SHALL distinguish `CLASSIFIED(scope_ids)` from `UNRESOLVED`.
For each scope, a path/ref exclusion SHALL first prove exclusion and a path/ref positive
SHALL next prove membership without semantic metadata. Only a still-undecided scope
whose positive selectors include project, tag, type, or class requires a valid companion.
If that companion is missing, malformed, unreadable, stale, or unsafe, the entire
artifact classification SHALL be `UNRESOLVED`. Missing companion metadata MUST NOT be
interpreted as empty metadata or an empty scope set. Every release, structured-read,
proposal, and grant-drift caller SHALL translate `UNRESOLVED` to the fail-closed
L0/missing contract. A policy containing only path/ref selectors remains classifiable
without a companion.

Semantic companions SHALL be located and bound by a closed artifact-class registry:
ordinary/media binaries use the sibling `<artifact-leaf>.md`; datasets use the unique
canonical card whose normalized `data_file` equals the artifact path and whose shape is either legacy `type: dataset` or `type: source` with explicit `source_type: dataset-export`; and a
scene frame uses its sibling `<frame-leaf>.md` inside the canonical
`<parent-media>.frames/` directory. Every valid companion SHALL carry a versioned
`governance_companion` descriptor with `state: classified`. Its immutable binding tuple
SHALL contain class, normalized artifact path, artifact SHA-256, and byte size; media also
contains media type and original filename; dataset also contains declared format; and a
scene frame also contains parent path/hash and an integer frame timestamp that agrees with
the canonical filename. That field SHALL be named `frame_timestamp_ms`, have integer
range `0..4_294_967_295`, and equal the integer milliseconds encoded in
`scene-<NNN>-t<frame_timestamp_ms>ms.jpg`. The descriptor SHALL also contain an exact `semantics` mapping
whose only keys are canonical string lists `projects`, `tags`, `types`, and `classes`;
these values classify the artifact and are distinct from metadata classifying the
companion page. The artifact and companion SHALL be canonical regular files read
from immutable snapshots. Zero/multiple dataset cards, a missing/incomplete descriptor,
binding mismatch, unsafe locator, changed bytes, or frame-parent/timestamp mismatch SHALL
be `UNRESOLVED` for every still-undecided semantic scope.

A descriptor with `state: classified`, a valid binding tuple, and all four semantic lists
deliberately empty SHALL be the only valid explicitly empty semantic companion.
Missing semantic keys without that marker MUST NOT mean empty. Pre-descriptor legacy media
sidecars—including the minimal stubs emitted by `preserve.py`, pending worker sidecars, and
completed transcripts—SHALL remain classifiable by path/ref policy only.

Backfill SHALL require an owner-authorized, receipt-first
`govern_memory(operation="backfill_companion")` proposal/commit. Version-1 input SHALL
contain exactly the artifact class, canonical artifact path, expected artifact SHA-256
and size, expected companion path and SHA-256, complete explicit four-list `semantics`,
and every class-specific binding field. The trusted adapter SHALL establish the local
owner. Companion-page tags/projects/type/classes, artifact metadata, policy, model text,
dataset rows, and media jobs MUST NOT authorize backfill or be inferred into artifact
semantics. Preview SHALL return the exact target descriptor and immutable identities;
commit SHALL revalidate them under the no-follow mutation boundary, write only the
descriptor, and record owner, proposal, prior/target hashes, and terminal outcome. It
SHALL preserve transcript/body, page-level metadata, and processing state, infer neither
non-empty nor empty semantics, remain idempotent only for the same committed input, and
refuse without partial metadata on ambiguity or drift.

Legacy scene-frame conversion SHALL parse finite non-negative `frame_ts`, calculate
`int(round(binary64(frame_ts) * 1000))` using round-to-nearest-ties-to-even, require the
bounded result to equal both the filename milliseconds and indexed frame timestamp, and
then store `frame_timestamp_ms`. A negative, non-finite, out-of-range, ambiguously
indexed, or disagreeing value SHALL refuse. After migration, the float SHALL NOT be
membership authority. New companions SHALL carry the descriptor at creation, except automatically captured dataset source cards whose artifact semantics have not been owner-reviewed; those cards SHALL omit the descriptor and remain unresolved for still-undecided semantic scopes.

#### Scenario: Selector kinds resolve membership

- **WHEN** a page matches a scope by any selector kind and is not caught by an
  `exclude` selector
- **THEN** the page is a member of that scope

#### Scenario: Policy change invalidates the memo

- **WHEN** the policy fingerprint changes
- **THEN** previously memoized membership is recomputed against the new policy

#### Scenario: Missing companion under semantic-only scope is unresolved

- **WHEN** a non-Markdown artifact has no companion Markdown and a non-excluded scope
  can select it only by project, tag, type, or class
- **THEN** membership is `UNRESOLVED`, every non-owning content/structured surface treats
  the artifact as L0/missing, and no caller receives an empty-scope open decision

#### Scenario: Malformed companion is also unresolved

- **WHEN** the required companion exists but cannot be parsed, read consistently, or
  tied to the artifact's immutable identity
- **THEN** membership fails closed identically to a missing companion

#### Scenario: Path-only policy needs no companion

- **WHEN** every scope uses only path/ref selectors and exclusions and a non-Markdown
  artifact matches none of them
- **THEN** membership is the classified empty set and the existing unmatched default
  applies

#### Scenario: Path exclusion proves semantic scope exclusion

- **WHEN** a non-Markdown artifact matches a scope's path/ref exclusion before semantic
  selectors are needed
- **THEN** that scope is proven excluded without requiring companion metadata

#### Scenario: A path match does not erase an unresolved sibling scope

- **WHEN** a path/ref selector proves membership in scope A while semantic-only scope B
  cannot be evaluated because companion metadata is missing
- **THEN** the artifact remains `UNRESOLVED` and a grant for A cannot make it public

#### Scenario: Explicitly empty companion is classified only when bound

- **WHEN** a non-Markdown artifact's canonical companion carries `state: classified`, no
  semantic selector values, and the complete tuple matches its immutable artifact snapshot
- **THEN** the semantic result is a valid explicit empty classification rather than
  unresolved

#### Scenario: Legacy preserve stub needs verified backfill

- **WHEN** a `preserve.py` legacy media stub or pending/completed sidecar lacks the complete
  descriptor under an applicable semantic-only scope
- **THEN** the artifact is unresolved until backfill verifies and binds the current binary,
  and backfill preserves the existing body and processing state

#### Scenario: Backfill semantics require explicit owner input

- **WHEN** a legacy companion page has tags/projects/type/classes but the reviewed
  backfill input omits or differs from its explicit artifact `semantics`
- **THEN** Exomem does not copy page metadata, refuses incomplete input, and writes no
  descriptor without an authenticated owner proposal and receipt-backed commit

#### Scenario: Backfill race is receipt-first and atomic

- **WHEN** artifact, companion, parent, dataset card, or expected bytes drift after
  preview or the operation crashes at any receipt/mutation boundary
- **THEN** state is exact prior or the one reviewed descriptor with a recoverable
  terminal; no partial tuple or inferred semantics appears

#### Scenario: Dataset card is unique and content-bound

- **WHEN** no card, two cards, or a stale card points at the same dataset, or its declared
  format/hash/size does not match the immutable data snapshot
- **THEN** dataset membership is unresolved and no row, aggregate, profile, count, or parse
  diagnostic crosses the boundary

#### Scenario: Scene frame binds its parent and timestamp

- **WHEN** a scene-frame companion's parent hash/path or timestamp differs from the frame
  directory, canonical filename, or current parent snapshot
- **THEN** both semantic membership and the frame-to-parent recall mapping are unresolved

#### Scenario: Legacy frame timestamp migration is canonical

- **WHEN** a legacy finite `frame_ts` rounds ties-to-even to the same bounded integer
  milliseconds encoded in the canonical filename and index
- **THEN** owner-reviewed backfill stores that exact `frame_timestamp_ms`; any rounding,
  range, filename, parent, or index mismatch refuses without choosing a source


#### Scenario: Dataset source card retains unreviewed semantics

- **WHEN** a new evidence upload produces a `type: source`, `source_type: dataset-export` card without a governance descriptor
- **THEN** its source type and tags govern the Markdown card and it remains eligible as a source citation
- **AND** raw dataset membership stays unresolved for still-undecided semantic scopes until owner-reviewed receipt-first backfill supplies a bound descriptor

#### Scenario: Mixed dataset card variants are ambiguous

- **WHEN** a legacy dataset page and an explicitly marked dataset source card both identify the same original
- **THEN** classification and companion backfill refuse the duplicate set without choosing one variant
