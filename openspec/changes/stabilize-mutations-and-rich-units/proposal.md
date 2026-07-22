## Why

Expected write contention is currently exposed to MCP clients as a protocol-level tool failure while cross-process status can simultaneously report the vault as free. This can trigger host-side tool disabling, makes the configured mutation timeout ineffective, lets validation-only replacement contend with writers, and leaves rich semantic-unit tags and context unavailable to retrieval despite accepting them at authoring time.

## What Changes

- Return deliberate operational refusals such as `MUTATION_BUSY` as structured MCP application failures while preserving native MCP errors for unexpected exceptions.
- Make mutation status and busy diagnostics observe the process-safe vault boundary and its current content-free holder across processes.
- Honor the configured mutation-acquisition timeout and keep `replace_memory(validate_only=true)` outside mutation authority.
- Keep media provenance hashing, bounded discovery scans, and derived index fanout outside the global mutation boundary while retaining writer fencing, idempotency, and per-artifact commit revalidation.
- Project rich semantic-unit `tags` and `context` into the same first-class fields used by compact units, indexes, filters, hits, exact reads, graph nodes, and context packs.
- Add deterministic cross-process, transport, replay-safety, and semantic-projection regressions without adding a server-side reasoning model or external dependency.

## Capabilities

### New Capabilities

- `semantic-unit-projection`: Rich and compact semantic units expose their authored category, tags, context, kind, and relations consistently through retrieval and graph projections.

### Modified Capabilities

- `hosted-mutation-safety`: The common per-vault mutation boundary gains process-safe holder observability and effective configured wait bounds for local replicas and hosted cells.
- `command-surface`: Validation-only replacement is read-only, and expected operational refusals use the shared structured application-error envelope on MCP without hiding unexpected faults.

## Impact

Affected areas include semantic-unit parsing and projection, structured filters and index fanout, media discovery/commit routing, mutation-lock metadata, writer-lease configuration and coordination status, generated MCP binding behavior, the portable skill contract, deployment diagnostics, and focused concurrency/transport tests. Public tool schemas remain unchanged; known operational failures change from MCP execution errors to normal tool results containing `success:false` and the existing stable error details.
