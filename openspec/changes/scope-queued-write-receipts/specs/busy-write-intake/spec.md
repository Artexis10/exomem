## ADDED Requirements

### Requirement: The originating login owns connector receipt disclosure

A signed personal receipt SHALL be available only to its originating owner account, client, and login, or to local administrative recovery. The issuer SHALL verify the login before connector release. Lookup SHALL give identical results for missing and out-of-scope receipts. Lists, counts, and notices SHALL apply the same authority.

#### Scenario: Another owner client checks a receipt

- **WHEN** a second client on the owner account asks for the first client's receipt
- **THEN** the result matches a missing reference
- **AND** the second client does not see the receipt in lists or counts or consume its failure notice

#### Scenario: A newly signed-in client retries an older login's save

- **WHEN** the same client resends a recognised write using the same original identity after signing in again
- **THEN** the service does not execute the save again within the retry window
- **AND** the response does not disclose the earlier login's reference, arguments, or outcome

#### Scenario: The issuer cannot confirm a previously valid login

- **WHEN** a connector has cached evidence for a login that the issuer cannot confirm
- **THEN** the connector receives no receipt reference, arguments, outcome, counts, or failure notice
- **AND** the next eligible save can name a failure that this reply could not disclose

#### Scenario: A token refresh keeps the same login

- **WHEN** a connector refreshes its token and the issuer confirms the same login
- **THEN** that login can still check its receipt
- **AND** a resend using the same original identity runs no new write
- **AND** the resend returns the receipt when the service can verify the stored login

#### Scenario: An owner recovers a legacy receipt

- **WHEN** an owner uses the existing local administrative intake command to open a receipt with no verified client binding
- **THEN** the receipt remains available with the recorded outcome and arguments
- **AND** an authenticated connector cannot look up the legacy receipt through a different ingress

#### Scenario: A refresh leaves older access identifiers outside the index

- **WHEN** a verified login's indexed results may omit older access identifiers
- **THEN** lists, counts, and notices mark their coverage partial and withhold queue position
- **AND** an exact receipt lookup can recheck an older access identifier with the issuer

#### Scenario: Two older receipts precede a token refresh

- **WHEN** a legacy receipt for a previous access identifier lacks a stored family
- **AND** the latest retained receipt for that write identity belongs to another login
- **THEN** the original login can recover the earlier receipt by exact reference
- **AND** a resend that cannot verify the earlier receipt runs no new write

#### Scenario: A legacy owner ingress asks for a signed receipt

- **WHEN** a legacy owner ingress without a client login asks for a personal connector's receipt
- **THEN** the lookup matches a missing reference
- **AND** the local administrative recovery command can still read the receipt

### Requirement: Legacy scope index maintenance stays off the save path

The idle drainer SHALL bound the lock wait and work time of each legacy scope index build. It SHALL delay retries. Saves SHALL NOT start a legacy scope index build.

#### Scenario: A legacy scope index build cannot finish

- **WHEN** the idle drainer cannot finish preparing the scope index within its allowance
- **THEN** it waits until its next allowed attempt
- **AND** saves do not start another scope index build

## MODIFIED Requirements

### Requirement: A resend of a queued save runs nothing

On a matching resend of an open save or a recognised terminal within its retry window, the intake SHALL preserve the original execution claim. Only current receipt authority can obtain its reference and outcome. An unreadable legacy store with no known claim keeps its existing fallback. The same identity with a different payload SHALL remain a different save.

#### Scenario: A resend while queued runs nothing

- **WHEN** the login that sent a queued save resends the same call with the same idempotency identity
- **THEN** the reply is the same queued receipt
- **AND** only one commit results

#### Scenario: A reused identity with a different payload is a different save

- **WHEN** a save is queued
- **AND** the caller sends a different payload with the same idempotency key
- **THEN** both saves are applied as separate commits

#### Scenario: A resend after an unresolved effect keeps the same failure receipt

- **WHEN** a queued save fails with `MUTATION_OUTCOME_UNKNOWN`
- **AND** after restart the originating login resends the same call within its retry window
- **THEN** the reply names the original failed receipt and reports that its commit effect is unknown
- **AND** the service does not execute a new write

#### Scenario: An older release created two receipts

- **WHEN** more than one retained terminal receipt matches the originating login's resend
- **THEN** the service returns the latest matching receipt still within the retry window
- **AND** when the owner dismisses that receipt, a resend can find the surviving receipt before and after restart

### Requirement: The intake stays off the uncontended path

An uncontended save SHALL skip intake storage except to check retries or notices or prepare legacy retry indexes. If retained failures cannot be checked, that first save SHALL return a retryable refusal with unknown effect.

An idle drainer SHALL poll at most once every few minutes. It SHALL preserve vault identities and SHALL write only to prepare the legacy scope index.

#### Scenario: An unreadable intake store does not block an ordinary save

- **WHEN** intake was unreadable at startup and the service knows of no matching receipt or legacy store
- **AND** an otherwise eligible save does not meet contention
- **THEN** the save commits as it would without the intake

#### Scenario: An idle legacy store defers the retry index build

- **WHEN** a service starts on a legacy store with retained failures and no open saves
- **THEN** startup does not scan the failures or write to the intake
- **AND** the first eligible save prepares the retry indexes and checks the retained failures before it can execute
