## Why

`connect_memory` and `adopt_vault` are write-CAPABLE tools, but their DEFAULT operations are read-only (`suggest-links`, `suggest-relations`, `graph-context`, `inbound-links`; scan-only adopt). Because they carry `cli_writes=True` at the command level, `command_surface.read_only` is False and the writer lease gates EVERY invocation — so a proposal-only or scan-only call fails `WRITER_COORDINATOR_UNAVAILABLE` when the coordinator is down, even though it performs no write. Read-only-ness is per-OPERATION, not per-command, and reads should stay available during a coordinator outage (a core failover promise). (Audit finding CDX-08, MED.)

Evidence: `src/exomem/commands.py` connect (~3554) / adopt (~3730); `cli_writes=True` (~4518-4533); the lease gate in `writer_lease.invoke` keys off `command.read_only`.

## What Changes

- Make the writer-lease gate consider the ACTUAL operation's read-only-ness rather than the command-level flag, so a read-only invocation of a write-capable tool bypasses the lease and succeeds during a coordinator outage.
- Keep write operations (`create-entity`, `accept-relation`, adopt write modes) gated by the lease as today.
- Preserve the MCP tool schema (do not split tools unless strictly necessary; if the surface must change, regenerate + review the schema-fidelity baseline deliberately).

## Capabilities

### New Capabilities

- `lease-operation-scope`: The writer lease gates a command only when the specific invoked operation writes to the vault; read-only operations of write-capable tools remain available when the lease coordinator is unreachable.

## Impact

Affects the lease gating in `src/exomem/writer_lease.py` / the command dispatch in `src/exomem/commands.py`; must keep `tests/test_mcp_schema_fidelity.py` green. New tests cover a read-only connect/adopt call succeeding with an unreachable lease URL while a write op still refuses.
