## Why

Local hybrid clients can silently multiply full Exomem model runtimes because setup and
the Claude plugin default to one stdio server per session even when one authenticated HTTP
service already exists. The same incident exposed two diagnostic blind spots and an
agent-facing reviewed-none handshake whose documented spelling and returned fields do not
match the executable contract.

## What Changes

- Make `exomem setup` prefer an explicitly or environmentally configured authenticated
  HTTP service for Claude Code and Codex while preserving stdio as an explicit manual
  fallback.
- Replace the Claude plugin's auto-starting full stdio core with an optional canonical
  HTTP placeholder while retaining its skills and hooks.
- Add conservative, reversible client-registration migration with URL validation,
  confirmation, and an explicit non-interactive replacement flag.
- Make editable-install doctor checks compare the active environment with the checkout's
  lock state offline and without mutation.
- Report Darwin physical footprint for Exomem processes, with labelled RSS compatibility
  and fallback, and use the same metric in the release resource gate.
- Return the reviewed-none commit hash under the public `relation_review_hash` name, teach
  the exact `reviewed_none` value, and accept the previously advertised hyphen spelling as
  a canonicalized compatibility alias.
- Document and test the post-upgrade route, environment, OAuth, and Mac acceptance flow.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `install-readiness`: Setup gains a service-aware client route and doctor gains truthful
  editable-environment and cross-platform process-memory checks.
- `agent-bootstrap-contract`: Reviewed-none guidance becomes executable from validation
  output without guessing field names or spellings.
- `command-surface`: Creation validation exposes an additive `relation_review_hash`, and
  the public boundary canonicalizes the previously advertised disposition alias.

## Impact

- Setup, client, and plugin configuration: `src/exomem/setup_wizard.py`,
  `src/exomem/client_config.py`, `src/exomem/package_skills.py`, Claude/Codex MCP
  registrations, the generated Claude plugin, and onboarding docs.
- Read-only diagnostics and release gates: `src/exomem/install_info.py`,
  `src/exomem/doctor.py`, and `scripts/verify-resource-envelope.py`.
- Semantic creation contract: `src/exomem/relation_review.py`,
  `src/exomem/semantic_contract.py`, `src/exomem/commands.py`, generated MCP schemas, and
  capability documentation.
- No new production dependency, model, daemon, unauthenticated transport, or stored-data
  migration. This is an explicit plugin behavior migration: plugin-only local users must
  run setup once to receive the manual stdio fallback; the plugin no longer launches a full
  Exomem core by itself, and live sessions must reload plugins or restart after updating.
