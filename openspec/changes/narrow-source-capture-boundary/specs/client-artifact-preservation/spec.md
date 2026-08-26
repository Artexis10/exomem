## MODIFIED Requirements

### Requirement: Safe bounded server-side retrieval

The system SHALL treat every `download_url` as hostile input. It MUST accept HTTPS only; reject userinfo, fragments, and any initial or redirected destination that resolves to a non-global IPv4 or IPv6 address; connect only to a validated resolved address while preserving the original hostname for TLS verification; send no Exomem credentials, cookies, or caller headers; and keep full URLs and query strings out of logs and results. It SHALL enforce bounded redirects, timeouts, item count, per-file bytes, and aggregate bytes while streaming to private temporary storage, with the streaming byte count authoritative over response headers. Retrieval SHALL complete before the canonical vault mutation boundary is acquired, for every command that stages client file handles and not only for the Evidence lane, so that no remote latency is served while the vault mutation lock is held.

#### Scenario: Public HTTPS attachment is staged

- **WHEN** an authenticated caller supplies a temporary HTTPS URL whose validated destination remains public and whose body stays within all limits
- **THEN** Exomem streams the body to private temporary storage without buffering the full artifact in memory
- **AND** retrieval occurs before the canonical vault mutation boundary is acquired

#### Scenario: Private or redirecting destination is refused

- **WHEN** a supplied URL or any redirect resolves to loopback, private, link-local, multicast, unspecified, reserved, or metadata address space
- **THEN** that file returns a stable safe-fetch failure without making a request to the forbidden destination or writing vault content

#### Scenario: Stream exceeds a bound

- **WHEN** the response header declares an oversized body or the streamed bytes exceed a per-file or aggregate cap
- **THEN** retrieval stops, temporary bytes are removed, the file reports a stable too-large failure, and no partial final artifact is published

#### Scenario: Source-lane retrieval runs outside the mutation lock

- **WHEN** a caller invokes `capture_source` with file handles rather than text
- **THEN** every handle is staged before the vault mutation boundary is acquired
- **AND** each staged file is committed under its own acquisition of that boundary
- **AND** a `capture_source` invocation carrying text instead of handles still runs under the boundary held across its whole leaf

#### Scenario: The wide-boundary kill switch still applies

- **WHEN** `EXOMEM_WIDE_MUTATION_BOUNDARY` is set and a caller stages file handles through either lane
- **THEN** the vault mutation boundary is acquired before retrieval begins
