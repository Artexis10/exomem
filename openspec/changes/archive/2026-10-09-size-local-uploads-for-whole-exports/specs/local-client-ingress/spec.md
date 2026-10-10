## MODIFIED Requirements

### Requirement: REST and upload accept the local token on local ingress

On local ingress the REST facade and `/upload` SHALL accept a valid local token as the
owner, even when the REST key or the upload token is unset, and SHALL keep their existing
gates on the public path. `/upload` SHALL keep its multipart form and its default evidence
lane. On local ingress a direct preserve or capture SHALL be capped by
`EXOMEM_LOCAL_UPLOAD_MAX_BYTES`, because loopback never crosses the proxy edge whose cap
sets the public `EXOMEM_UPLOAD_MAX_BYTES`, and a held upload SHALL stay within what its
redeeming command can fetch; the public path SHALL keep the public cap. `/upload` SHALL
accept `raw_protection=1` for a direct preserve or capture and SHALL refuse it on a held
upload. `exomem attach <file>` SHALL send the file's bytes to the local `/upload` with a
local token and print the returned handle, and `--raw-protection` SHALL send that field;
no tool argument SHALL name a local path.

#### Scenario: Upload over local ingress
- **WHEN** `exomem attach` sends a file with a valid local token
- **THEN** the service preserves the bytes and the command prints the handle

#### Scenario: A whole export over the public cap
- **WHEN** a local client uploads a file larger than the public cap and smaller than the local cap, with `raw_protection=1`
- **THEN** the service preserves the exact bytes as an owner-only original
- **AND** the same bytes through the public path are refused as too large

#### Scenario: REST over local ingress
- **WHEN** a local client posts to `/api/<tool>` with a valid local token
- **THEN** the call runs as `owner-local`
