## Context

See proposal.md for the reported failure. The canonical dispatcher already binds the addressed vault and verified principal. Local CLI/stdio and the shared REST key identify the local owner; OAuth and gateway callers have canonical hashed identities. These identities are deliberately distinct.

## Goals / Non-Goals

The normal agent surface must save a user's engagement choice and immediately report the same effective contract as bootstrap. Settings must not grant additional authority or change another principal or vault. This change does not link accounts, reconfigure compute mode, or change standalone hooks' existing machine-level inputs.

## Decisions

### One small preference file per vault and principal

Place records in the existing external per-vault state directory, with filenames derived from the bound principal's hash. A request never supplies an audience, filesystem destination or alternate vault. This avoids granting authenticated users permission to overwrite a shared vault-wide preference. Reject unresolved identities. Local operator configuration remains an explicit legacy fallback; no existing file is rewritten or migrated automatically.

Use atomic replacement under the canonical mutation boundary. An inspect returns a content revision; set requires that revision, preventing stale agents from overwriting a later choice. Separate files avoid a read/merge/write race between different users. Invalid or corrupt preference records fail configuration writes closed and appear as an unavailable preference during read resolution.

### Resolve once in the current request context

Bind the addressed vault and client surface around canonical invocation. Existing prominence consumers then resolve the same identity-specific preference, including bootstrap, workflow effective capture and the delegation envelope. Precedence is valid operator environment override, saved identity preference, existing explicit machine configuration, then the client surface default. The legacy machine setting remains visible as `config` for compatibility; the new setter never changes it.

Use known client identity/transport for surface defaults. Explicit EXOMEM_SURFACE and hosted-cell signals remain authoritative. Unknown identities retain an honest generic default rather than claiming that every HTTP caller lacks hooks.

### Canonical inspect/set operation

`configure_memory(action="inspect"|"set", prominence=None, expected_revision=None)` exposes only prominence. Inspect is read-only; set requires a canonical level or existing alias and an exact inspected revision. Return effective level, stored preference, source, scope, revision and operator-override state without exposing other preferences or local filesystem paths. Reject a requested level that conflicts with a valid operator override before writing, with an actionable override error. The agent follows the returned contract immediately; later calls and clients with the same verified identity observe the saved choice without restarting.

Keep `exomem prominence` as the legacy local machine control. The generated canonical CLI supports the new authenticated/vault-scoped operation; docs explicitly distinguish these scopes rather than silently changing an old command's meaning.

## Risks / Trade-offs

- Different credentials for the same person can yield distinct principals → report identity-scoped persistence and do not claim account linking.
- Local hooks lack a vault/principal binding → retain their existing configuration and document that limitation.
- Environment override shadows a user preference → inspect reports it, and conflicting sets fail before mutation.
- New tool affects several exported surfaces → test the registry, classification, authorization, MCP/REST schema and terminal receipt, then regenerate only canonical metadata.
- Session instructions can be cached by a client → the setter returns the current contract and instructs immediate adoption; a fresh bootstrap reports the same choice.

## Migration Plan

The absence of preference state preserves existing explicit operator configuration and generic defaults. Deploy the additive tool and resolver changes; refresh clients' tool discovery where required. No service environment or live preference is changed by deployment. Rollback leaves the new files inert; existing machine settings retain their original bytes.
