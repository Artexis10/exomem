## ADDED Requirements

### Requirement: Self-hosted server serves its own Claude plugin
A self-hosted server with a configured public origin SHALL serve, at `GET /claude-plugin.zip` and without sign-in, a Claude plugin archive that connects to that origin's `/mcp` endpoint. A server without a public origin SHALL NOT serve the route.

#### Scenario: Server without a public origin
- **WHEN** a server runs without a public origin
- **THEN** `/claude-plugin.zip` is not served

#### Scenario: Origin that cannot produce a valid plugin
- **WHEN** the configured origin is too long for claude.ai's description limit
- **THEN** `/claude-plugin.zip` answers 503 and the server log names the reason

### Requirement: The served plugin carries the release's skills and hooks
The served archive SHALL carry the release's hooked edition skills and hooks with identical bytes, and SHALL name neither the placeholder origin nor the release placeholder. Its manifest version SHALL be the server's installed release. Its plugin name SHALL derive from the origin's host, its display name SHALL be "Evermatter", and its manifest SHALL pass the same limits as the generated Claude editions.

#### Scenario: Download from a server with a public origin
- **WHEN** a client requests `/claude-plugin.zip` from a server whose public origin is `https://kb.example.com`
- **THEN** the response is a zip attachment named `evermatter-kb-example-com.zip`
- **AND** its `.mcp.json` declares one HTTP server whose URL is `https://kb.example.com/mcp`
- **AND** its hook and skill files equal the hooked edition's bytes
- **AND** no file names the placeholder origin
- **AND** its manifest name is at most 64 lowercase letters, digits and hyphens and its description is at most 500 characters

### Requirement: Remote setup points at the plugin
`exomem setup --remote` SHALL end by telling the user to download the server's plugin, upload it in Claude and connect from the plugin's Connectors tab, and SHALL still show the custom-connector URL as the alternative.

#### Scenario: Successful remote setup
- **WHEN** remote setup completes its preflight
- **THEN** its output names `<origin>/claude-plugin.zip` and the `<origin>/mcp` connector URL
