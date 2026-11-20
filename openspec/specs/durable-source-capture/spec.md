# durable-source-capture Specification

## Purpose
Let service callers reconcile text Source captures from canonical storage after bounded invocation replay expires or a process restarts.

## Requirements

### Requirement: Caller identity binds an immutable text capture

`capture_source` SHALL accept optional `source_identity` with exactly three nonempty opaque strings: `system`, `record_id`, and `revision`.
Identity SHALL be vault-local and independent of invocation keys. Files and adoption with this identity MUST be refused before retrieval or writes.
A capture that carries identity SHALL be decided by that identity alone and SHALL NOT be reused by payload. Omitting identity SHALL leave the capture to payload reuse under `duplicate-handling`.

#### Scenario: Independent records contain identical text
- **WHEN** two captures have different identities and identical authored inputs
- **THEN** each capture creates its own Source and stable reference

#### Scenario: Revisions arrive out of order
- **WHEN** a caller captures two revisions of one record in any order
- **THEN** both remain separate immutable Sources
- **AND** neither capture infers chronology, predecessor links or retirement

#### Scenario: Identity accompanies an attachment
- **WHEN** a caller supplies source identity with files or adoption
- **THEN** capture refuses with `SOURCE_IDENTITY_TEXT_ONLY` before fetching or writing

#### Scenario: Identity-bearing text equals an earlier payload
- **WHEN** a caller supplies a new identity with text that equals the payload of an existing Source
- **THEN** capture creates its own Source and returns no `duplicate_of`

### Requirement: Durable reconciliation compares original authored inputs

The initial canonical Source write MUST store typed identity and a digest of normalized original text, title, kind, URL, tags, reason, domain, projects, requested slug and raw protection.
Retry IDs, response detail, compilation hints, timestamps, allocated paths and server IDs MUST NOT affect that digest.
Later metadata updates MUST preserve the original digest.

#### Scenario: Capture response is lost beyond replay retention
- **WHEN** an identical capture arrives after restart or replay expiry with a fresh invocation key
- **THEN** the same stable Source reference returns with explicit `reused` outcome
- **AND** no second Source or capture activity is written

#### Scenario: Metadata changed after capture
- **WHEN** a Source gains compilation back-references before the caller repeats its original capture
- **THEN** reconciliation uses the original capture digest and returns that Source

#### Scenario: Authored inputs conflict
- **WHEN** the same identity arrives with different normalized authored inputs
- **THEN** capture refuses with `SOURCE_IDENTITY_CONFLICT` without writes

### Requirement: Canonical Sources decide identity uniqueness

Identity lookup and creation SHALL share the existing mutation guard.
Canonical Source and Evidence pages SHALL be the authoritative lookup, regardless of search, graph or sidecar state.
Unreadable or malformed candidate pages MUST return `SOURCE_IDENTITY_UNAVAILABLE`, because an incomplete scan cannot prove absence.
Canonical receipts MUST be validated regardless of Markdown filename, including `index.md` and the root Sources catalog path.
After bound originals are classified, readable frontmatter-free legacy pages outside the reserved root Sources catalog SHALL be excluded from receipt candidates.
Capture SHALL report each excluded legacy page for curation through its warnings, including when it reuses an existing capture.
The maintained root Sources catalog SHALL remain excluded without a curation warning.
If that reserved path lacks either maintained section header, lookup MUST return `SOURCE_IDENTITY_UNAVAILABLE` before capture can rewrite it.
Stale catalog rows MUST NOT determine identity uniqueness or absence.
Catalog recognition does not establish ownership or distinguish a former receipt whose metadata and bytes were replaced with a valid catalog.
Legacy Sources MUST NOT be backfilled or deduplicated implicitly.

#### Scenario: Concurrent requests use different invocation keys
- **WHEN** two callers concurrently capture the same identity and inputs with different invocation keys
- **THEN** exactly one Source is created and the other call returns it as reused

#### Scenario: Derived indexes lag
- **WHEN** a matching Source exists but its derived indexes have not caught up
- **THEN** reconciliation returns the canonical Source reference without another capture

#### Scenario: Canonical lookup is incomplete
- **WHEN** a canonical Source candidate cannot be read or parsed
- **THEN** capture returns the typed unavailable error without claiming absence or writing

#### Scenario: A Source is promoted into Evidence
- **WHEN** a captured Source is explicitly promoted and its original capture is repeated after replay expires
- **THEN** capture returns the same stable reference at the promoted path without reclassification
- **AND** a promoted page named `index.md` remains authoritative

#### Scenario: A Markdown original forges receipt and companion metadata
- **WHEN** a bound Markdown original contains capture identity or a declaration that targets another canonical page
- **THEN** its metadata cannot provide receipt truth or suppress that canonical page
- **AND** lookup gives the same result in either enumeration order

#### Scenario: Legacy originals coexist with durable receipts
- **WHEN** one readable frontmatter-free legacy page exists beside valid receipt pages
- **THEN** identity-enabled capture creates or reuses its correct Source and reports the legacy page for curation
- **AND** the legacy page remains unchanged

### Requirement: Identity lookup requires whole-namespace content authority

Before identity lookup, callers MUST satisfy existing whole-namespace content authority and the existing creation boundary.
The authority proof MUST include current RAW authority, connector ceiling and local disclosure policy.
For a nonempty local policy, all declared scopes MUST permit full disclosure for the current audience and purpose, with standing grants applied.
This conservative proof MUST refuse even when a restricting scope covers material outside Sources and Evidence.
Exact-item session grants MUST NOT establish whole-namespace authority.
Blocked policy, unavailable purpose authority and malformed verified authorization-session context MUST return the same forbidden outcome.
The system MUST recheck authority before committing.
The same restriction MUST precede saved invocation replay and invocation-key mismatch checks.
Restricted callers MUST receive `SOURCE_IDENTITY_FORBIDDEN` without learning whether the identity exists.
Ordinary capture authority SHALL remain unchanged.

#### Scenario: A restricted caller probes an identity
- **WHEN** a caller without whole-namespace content authority submits an identity
- **THEN** the same forbidden outcome returns for absent, matching and conflicting captures
- **AND** no identity existence or stored details are disclosed

#### Scenario: Authority changes before commit
- **WHEN** whole-namespace authority is revoked after lookup and before commit
- **THEN** capture refuses without writing

#### Scenario: An unrelated namespace is restricted
- **WHEN** a local policy restricts an unrelated Notes scope for the caller
- **THEN** identity-enabled capture returns `SOURCE_IDENTITY_FORBIDDEN` even if the capture lanes have no restriction
- **AND** ordinary capture retains its existing authority rules

#### Scenario: Authority narrows after an earlier capture
- **WHEN** a restricted caller repeats an existing invocation key with identical or changed capture inputs
- **THEN** the forbidden outcome precedes replay and mismatch responses without disclosing prior capture details

### Requirement: A corrected Source preserves the previous original

`capture_source` SHALL accept a correction naming one predecessor, its historical page hash and a reason.
It SHALL create one successor and an immutable correction receipt atomically, without rewriting predecessor bytes.
The predecessor SHALL have an existing canonical identity and satisfy current point authority.
A stale hash or an existing successor SHALL refuse the correction without an orphan capture.

#### Scenario: A corrected original replaces a retyped capture

- **WHEN** a caller captures a corrected original with the predecessor's current historical hash
- **THEN** the old Source and its original remain byte-identical
- **AND** the successor and receipt explicitly identify the correction
- **AND** two concurrent corrections of that predecessor cannot both commit

### Requirement: Source consumers follow the canonical correction chain

Page reads, compiled-source admission and compilation proposals SHALL resolve a cited Source to its current successor.
Custody-page downloads SHALL resolve its current original; explicit artifact paths SHALL remain exact.
The resolver SHALL authorize every Source independently and use canonical receipts rather than derived graph currency.
Historical reads SHALL remain available with `follow_corrections=false`; exact unit reads SHALL remain exact.

#### Scenario: An existing citation selects the corrected original

- **WHEN** a client reads or compiles from a corrected Source citation
- **THEN** it receives the current Source identity and current contents with an explicit resolution envelope
- **AND** existing compiled citations remain unchanged

#### Scenario: A successor is withheld or the chain is invalid

- **WHEN** a successor is withheld, missing, cyclic or malformed
- **THEN** current-source resolution returns the ordinary content-free unavailable response
- **AND** it exposes no successor reference, reason, hash or hop count
- **AND** it does not present a known historical original as current

#### Scenario: A vault export retains history

- **WHEN** a vault with corrected Sources is exported and restored
- **THEN** every Source version, original artifact and correction receipt remains preserved

### Requirement: A capture whose payload is already stored reuses the earlier Source

A `capture_source` call without `source_identity` SHALL, within one acquisition of the vault mutation boundary, look up the payload of its text through the duplicate-handling index. The payload SHALL be the text exactly as the writer would store it. When a live Source of the same protection class that the caller can read in full holds an identical payload, the call SHALL write no Source, no index or log entry and no activity record. It SHALL return that Source's stable reference with `capture_outcome: "reused"` and `duplicate_of` naming its path and ref, and SHALL leave that Source unchanged. The result SHALL also state the earlier Source's title, kind and URL, so the caller can see what it matched. The call SHALL NOT merge the new call's title, tags, reason, kind, URL or any other metadata into the earlier Source. The result SHALL name, in `fields_not_applied`, each caller-provided field whose value differs from the earlier Source's value, with the provided value and the kept value, so the caller sees what was not applied. A field that the caller omitted, or that equals the earlier value, SHALL NOT be listed.

File handles keep the existing `already_stored` outcome, which the preservation requirements extend across the vault. A call that carries `source_identity` is decided by that identity alone and SHALL NOT be reused by payload. This requirement concerns payload identity only. It writes no field into a Source that lacks one, and it leaves implicit deduplication of existing Sources to the reviewed fold.

#### Scenario: The same text is captured twice

- **WHEN** a caller captures a text and later captures the same text again without an identity
- **THEN** the second call returns the first Source's reference with `capture_outcome: "reused"` and `duplicate_of`
- **AND** exactly one Source file exists

#### Scenario: Identical text under a new title is still the same payload

- **WHEN** a caller captures stored text again with a different title and different tags
- **THEN** the call returns the earlier Source with `duplicate_of` and its earlier title
- **AND** `fields_not_applied` names `title` and `tags`
- **AND** the earlier Source is byte-identical to before the call

#### Scenario: A differing URL is named, not dropped silently

- **WHEN** a caller captures stored text again with a different URL, a different title and an extra metadata field
- **THEN** the result lists `url`, `title` and that field in `fields_not_applied`, each with the provided and the kept value
- **AND** a field that equals the earlier Source's value is absent from the list
- **AND** the earlier Source is byte-identical to before the call

#### Scenario: A capture carries an identity

- **WHEN** a caller captures text that equals the payload of an existing Source and supplies a new `source_identity`
- **THEN** the call creates its own Source and no `duplicate_of` is returned

#### Scenario: A legacy Source is found without being rewritten

- **WHEN** a capture repeats the text of a Source written before the payload index existed
- **THEN** the call reuses that Source
- **AND** the Source's bytes are unchanged

### Requirement: A shared URL with different text is surfaced and never blocks

For a new capture that carries a URL, `capture_source` SHALL look up, in the same boundary hold as the write, live Sources that the caller can read and that share the capture's canonical URL. The canonical form SHALL lowercase the scheme and host, drop a default port and the fragment, and keep the path and query exactly. When one or more such Sources exist and none holds an identical payload, the result SHALL carry a `same_url` advisory naming up to five of them (ref, path, capture date, title) and their total count. The capture SHALL still be written, and nothing SHALL be reused, superseded or changed. When an identical payload exists, reuse applies and no `same_url` advisory is added. Choosing between the captures, or recording a correction, remains the caller's decision.

#### Scenario: A page changed since its first capture

- **WHEN** a caller captures a URL whose text differs from the earlier capture of the same URL
- **THEN** a new Source is written
- **AND** the result carries `same_url` naming the earlier Source

#### Scenario: A different query is a different URL

- **WHEN** a caller captures a URL that differs from an earlier URL only in its query string
- **THEN** no `same_url` advisory is returned

#### Scenario: A withheld Source is not named

- **WHEN** the only other Source with that URL is withheld from the caller
- **THEN** the result equals the result of a vault without that Source

### Requirement: A capture may declare passages with caller keys

`capture_source` SHALL accept optional `passages`: an ordered list of objects, each with `key` and `text`.
`passages` and `content` SHALL exclude each other.
The capture SHALL render the Source body as the passage texts joined by one blank line, so that the Source reads as an ordinary text capture.
The capture SHALL store each passage's text byte for byte as the caller sent it.
The capture SHALL refuse, before any write:

- more than 2,000 passages;
- a key that is not a string of 1 to 128 bytes as UTF-8, or that repeats within the capture;
- passage text that is empty, contains a blank line or a carriage return, or has white space at its start or end.

Each refusal SHALL name the passage ordinal, the field and the limit or the correction.

#### Scenario: Passages replace content

- **WHEN** a caller supplies `passages` and no `content`
- **THEN** the capture creates one Source whose body holds the passage texts in order

#### Scenario: Both inputs are refused

- **WHEN** a caller supplies `passages` and `content` together
- **THEN** the capture refuses before any write and names both fields

#### Scenario: A repeated key is refused

- **WHEN** two passages of one capture have the same key
- **THEN** the capture refuses before any write
- **AND** the error names both passage ordinals and the field

#### Scenario: Text that capture would have to change is refused

- **WHEN** one passage's text ends with white space
- **THEN** the capture refuses before any write
- **AND** the error names the passage ordinal and tells the caller to trim the text

### Requirement: Declared passages are original authored inputs

The capture input digest SHALL cover each declared passage's key and text, in order.
A repeated capture with the same identity and the same passages SHALL reuse the Source.
A repeated capture with the same identity and different keys or texts SHALL be the same conflict as different text.

#### Scenario: A retry reuses the Source

- **WHEN** a caller repeats a capture with the same identity, keys and texts
- **THEN** the capture returns the existing Source with `capture_outcome: reused`

#### Scenario: A changed key conflicts

- **WHEN** a caller repeats a capture with the same identity and one changed key
- **THEN** the capture reports the identity conflict and changes nothing

### Requirement: Explicit attempts bind exact authored input

Optional capture attempts SHALL use fresh server-issued handles bound to exact typed authored input, Source identity and authenticated principal scope.
The binding SHALL precede input-changing normalization and survive credential rotation.
Reservation SHALL retain no capture content.
Ordinary capture without an attempt SHALL retain its existing behavior.

#### Scenario: Canonically equivalent strings differ

- **WHEN** a reserved capture is submitted with different UTF-8 strings that Unicode normalization would equate
- **THEN** input binding refuses it before canonical writes

#### Scenario: A cell bearer rotates

- **WHEN** the authenticated cell-service identity remains unchanged after bearer rotation
- **THEN** its attempt remains bound to that same identity without requiring a gateway origin key

### Requirement: Attempt admission and settlement share canonical exclusion

Commit SHALL validate an existing attempt inside the actual non-borrowed canonical mutation boundary and retain it through terminal persistence.
Settlement SHALL use that same boundary to close admission.
Missing, expired, cancelled or consumed attempts MUST NOT recreate execution.
Admission expiry MUST NOT cancel accepted execution.

#### Scenario: A delayed request arrives after settlement

- **WHEN** settlement closes an unused attempt before its delayed capture reaches the boundary
- **THEN** the later capture refuses without a canonical write

#### Scenario: Accepted execution outlives the caller

- **WHEN** a capture holds the boundary after its caller stops waiting
- **THEN** settlement reports pending or waits within its bounded admission policy
- **AND** it cannot claim admission closed before that capture leaves the boundary

#### Scenario: A nested callback settles its ancestor

- **WHEN** an executing capture synchronously settles its own ticket, including through another nested capture
- **THEN** settlement reports pending without cancelling the ancestor
- **AND** inherited context on another thread does not impersonate ownership of the boundary

### Requirement: Closure reports its actual custody scope

Reservation and settlement SHALL identify observed cell, replica, persistent ledger instance and canonical-boundary identity.
The consumer SHALL distinguish local admission closure from past capture outcome and current Source presence.
Changed or unproved custody SHALL remain unknown.
Settlement MUST NOT scan Sources or execute recovery capture to manufacture a terminal.

#### Scenario: A terminal receipt is unavailable

- **WHEN** the same authoritative scope closes an attempt without a retained success receipt
- **THEN** admission is closed here and past outcome remains unknown
- **AND** complete inventory supplies current Source state separately

#### Scenario: The runtime ledger was replaced

- **WHEN** a new database has a different ledger-instance identity
- **THEN** settlement does not claim closure for a receipt from the earlier ledger

#### Scenario: An earlier issuer remains alive but idle

- **WHEN** settlement closes its attempt under the same canonical boundary and unchanged ledger scope
- **THEN** a later request from that issuer cannot execute the closed attempt
- **AND** closure requires no process-retirement assertion or newer lease token

### Requirement: Protocol observations bypass generic replay

Reservation, settlement and ticketed capture SHALL bypass explicit and implicit generic replay before saved-terminal lookup.
They SHALL preserve ordinary authority, mutation fencing and terminal filtering.
The protocol SHALL retain its own exact terminals and collect eligible history in indexed bounded batches without permanent identity tombstones.

#### Scenario: A caller repeats a reservation request

- **WHEN** a previous reservation was consumed or cancelled
- **THEN** a new reservation call allocates a new handle rather than replaying the old one

#### Scenario: A caller polls settlement with the same invocation key

- **WHEN** an earlier settlement observed pending and the capture has since completed
- **THEN** settlement observes the current state rather than replaying pending

#### Scenario: Collected history no longer has a receipt

- **WHEN** a closed attempt's retained row has been collected
- **THEN** local closure remains provable under its unchanged authoritative scope
- **AND** its past outcome remains unknown without recreating the attempt

### Requirement: Explicit incarnation erasure closes old capture replay

Owner-enabled erasure SHALL be an explicit exception to ordinary Source preservation. Old capture terminals MUST NOT replay a retired reference as a live Source. Fresh capture of its logical identity SHALL allocate a fresh reference. Erasure MUST NOT infer a correction or keep a permanent logical-identity tombstone.

#### Scenario: Retired capture is retried
- **WHEN** a caller repeats an earlier capture after that Source incarnation completes erasure
- **THEN** a saved terminal cannot present the retired Source as live
- **AND** a fresh admitted capture can create the logical identity with a new Source reference

### Requirement: Capture settlement distinguishes admission from custody

Settlement SHALL preserve its scoped admission and past-outcome guarantees while reporting custody completeness, owned allocations and attempt residue separately. Ordinary settlement MUST NOT destroy an uncertain capture's preserved original. Explicit cleanup SHALL retain unresolved custody beyond normal retry-cache expiry.

#### Scenario: Capture outcome is unknown but allocation is known
- **WHEN** a capture loses its terminal after allocating a Source reference
- **THEN** settlement can report unknown past outcome and the proved owned allocation without claiming publication or absence

#### Scenario: Ordinary settlement reports residue
- **WHEN** a caller settles an interrupted attempt without requesting cleanup
- **THEN** admission closes within its proved scope and retained original bytes remain available for recovery
