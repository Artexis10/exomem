## Why

Release 0.76.0 cannot complete an OAuth authorization with a conforming client.
The client reports an RFC 9207 issuer mismatch and refuses the response, so
reauthentication fails and the connector cannot be used.

The upgrade to FastMCP 4 is what exposed it. That framework advertises
`authorization_response_iss_parameter_supported` unconditionally in the
authorization server metadata, which under RFC 9207 obliges every authorization
response from this server to carry an `iss` parameter. Exomem's session proxy
overrides the identity-provider callback and builds the client redirect by hand,
emitting only `code` and `state` on success and only the error parameters on
failure. FastMCP 3 never advertised the capability, so the same override was
silently tolerated; on FastMCP 4 the server now promises something it does not do.

The framework already provides the helper that gets this right, including the
case where a registered redirect URI carries its own `iss`. The override should
use it rather than reimplement the invariant.

No spec stated this contract, which is why the regression passed review and a
release. Stating it is the durable half of the fix.

## What Changes

- Emit the RFC 9207 issuer on both client-facing redirects from the overridden
  callback, success and error alike, through the framework's redirect helper.
- State the contract in the protocol compatibility capability, so a future
  override cannot drop the parameter without failing a requirement.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp-protocol-compatibility`: authorization responses must carry the issuer
  the discovery document advertises.
