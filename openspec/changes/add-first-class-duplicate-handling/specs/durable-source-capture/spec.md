## MODIFIED Requirements

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

## ADDED Requirements

### Requirement: A capture whose payload is already stored reuses the earlier Source

A `capture_source` call without `source_identity` SHALL, within one acquisition of the vault mutation boundary, look up the payload of its text through the duplicate-handling index. The payload SHALL be the text exactly as the writer would store it. When a live Source of the same protection class that the caller can read in full holds an identical payload, the call SHALL write no Source, no index or log entry and no activity record. It SHALL return that Source's stable reference with `capture_outcome: "reused"` and `duplicate_of` naming its path and ref, and SHALL leave that Source unchanged. The result SHALL also state the earlier Source's title, kind and URL, so the caller can see what it matched. The call SHALL NOT merge the new call's title, tags, reason, kind or URL into the earlier Source.

File handles keep the existing `already_stored` outcome, which the preservation requirements extend across the vault. A call that carries `source_identity` is decided by that identity alone and SHALL NOT be reused by payload. This requirement concerns payload identity only. It writes no field into a Source that lacks one, and it leaves implicit deduplication of existing Sources to the reviewed fold.

#### Scenario: The same text is captured twice

- **WHEN** a caller captures a text and later captures the same text again without an identity
- **THEN** the second call returns the first Source's reference with `capture_outcome: "reused"` and `duplicate_of`
- **AND** exactly one Source file exists

#### Scenario: Identical text under a new title is still the same payload

- **WHEN** a caller captures stored text again with a different title and different tags
- **THEN** the call returns the earlier Source with `duplicate_of` and its earlier title
- **AND** the earlier Source is byte-identical to before the call

#### Scenario: A capture carries an identity

- **WHEN** a caller captures text that equals the payload of an existing Source and supplies a new `source_identity`
- **THEN** the call creates its own Source and no `duplicate_of` is returned

#### Scenario: A legacy Source is found without being rewritten

- **WHEN** a capture repeats the text of a Source written before the payload index existed
- **THEN** the call reuses that Source
- **AND** the Source's bytes are unchanged

### Requirement: A shared URL with different text is surfaced and never blocks

After writing a new capture that carries a URL, `capture_source` SHALL look up live Sources that the caller can read and that share the capture's canonical URL. The canonical form SHALL lowercase the scheme and host, drop a default port and the fragment, and keep the path and query exactly. When one or more such Sources exist and none holds an identical payload, the result SHALL carry a `same_url` advisory naming up to five of them (ref, path, capture date, title) and their total count. The capture SHALL still be written, and nothing SHALL be reused, superseded or changed. When an identical payload exists, reuse applies and no `same_url` advisory is added. Choosing between the captures, or recording a correction, remains the caller's decision.

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
