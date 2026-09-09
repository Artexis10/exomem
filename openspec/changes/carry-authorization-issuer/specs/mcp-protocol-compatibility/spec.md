## ADDED Requirements

### Requirement: Authorization responses carry the advertised issuer

While the authorization server metadata advertises
`authorization_response_iss_parameter_supported`, every client-facing
authorization response the server issues SHALL carry an `iss` parameter equal,
byte for byte, to the `issuer` value in that metadata. This SHALL hold for
successful responses carrying a code and for error responses alike, and SHALL
survive any override of the identity-provider callback. The parameter SHALL
appear exactly once even when the registered redirect URI already carries an
`iss` of its own.

#### Scenario: Successful authorization reaches a conforming client

- **WHEN** an identity-provider callback completes and the server redirects to the
  client's registered redirect URI
- **THEN** the redirect carries the code, the original state, and the advertised issuer
- **AND** a client validating RFC 9207 completes the exchange

#### Scenario: Authorization error reaches a conforming client

- **WHEN** the identity provider returns an error and the server redirects it onward
- **THEN** the redirect carries the error, the original state, and the advertised issuer

#### Scenario: Redirect URI already carries an issuer

- **WHEN** the registered redirect URI contains its own `iss` query parameter
- **THEN** the response carries the server's advertised issuer exactly once

#### Scenario: Advertisement and behavior cannot drift apart

- **WHEN** the served authorization server metadata is read
- **THEN** it advertises support for the issuer parameter
- **AND** that advertisement is verified alongside the responses that must honor it
