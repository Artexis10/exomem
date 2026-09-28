## MODIFIED Requirements

### Requirement: REST-First Transport When Configured And Reachable

The hook SHALL attempt exactly one POST request under the configured-port
requirement below (`/api/ask_memory`, `detail=compact`, `mode=hybrid`, `limit=3`,
`Authorization: Bearer <EXOMEM_REST_API_KEY>`) with a socket timeout of about
2 seconds, before
considering any other transport, whenever `EXOMEM_RETRIEVE_INJECT` is truthy and
`EXOMEM_REST_API_KEY` is present in the hook's own environment. The hook SHALL
treat any failure of that request (connection error, timeout, non-200 status,
malformed JSON, or an envelope with `success: false`) as "REST unreachable."

When a local client token is configured (`EXOMEM_LOCAL_TOKEN_FILE` names a readable,
non-empty file) and a local port resolves (`EXOMEM_LOCAL_PORT` in the hook's environment,
else in the managed install's `service.env`), the hook SHALL first make the same POST to
`http://127.0.0.1:<local port>` with that token, never to `EXOMEM_HOST`. For one release,
any failure of that local request SHALL fall through to the lifted-key request above under
the same wall-clock budget, and the log line SHALL name the rung that answered as `local`
or `rest`.

#### Scenario: REST configured and reachable

- **WHEN** `EXOMEM_RETRIEVE_INJECT` is truthy, `EXOMEM_REST_API_KEY` is set, and
  the local REST facade answers with `{"success": true, "data": [...compact
  hits...]}`
- **THEN** the hook's `additionalContext` includes a routing-stub block built
  from those hits
- **AND** the CLI transport is never attempted

#### Scenario: REST configured but unreachable, CLI not opted in

- **WHEN** `EXOMEM_RETRIEVE_INJECT` is truthy, `EXOMEM_REST_API_KEY` is set, the
  REST request fails (any of: connection error, timeout, non-200, malformed
  JSON, `success: false`), and `EXOMEM_RETRIEVE_INJECT_CLI` is not set truthy
- **THEN** the hook falls back to today's reminder-only `additionalContext`
- **AND** no CLI subprocess is attempted

#### Scenario: The local listener answers with the local token

- **WHEN** a local token file and a local port are configured and the local listener
  answers successfully
- **THEN** the hits come from `127.0.0.1:<local port>` with the local token
- **AND** neither the lifted key nor the CLI transport is used

#### Scenario: The local listener is not live yet

- **WHEN** a local token file and a local port are configured but the local request fails
- **THEN** the hook makes today's lifted-key request within the remaining budget
