## Why

The 2026-08-29 incident cost two days of a dead personal cell. The cause was not a
retrieval defect: a user-token flow created the external state root carrying the
creating token's ACE, and the LocalSystem service then fail-closed every catalog
proof, lexical repair and graph recovery with `WindowsRuntimeDaclError`, refusing
`catalog_proof_incomplete` forever at ~0 CPU. Nothing in `doctor` said why —
`state.placement` reported a healthy "migration completed" the whole time, because
placement and *access* were never separated.

Issue #933 records four measured manifestations. Two more were found while fixing
it: `_check_rebuild_temp_orphans` crashes on an unlistable root one check after the
cause is correctly reported, and `is_dir()` does not screen that case out because
stat succeeds through the parent's traverse while `iterdir` is refused.

The underlying rule is that the private-DACL validator is **token-relative**:
`_windows_private_dacl_trustees(sid)` yields `(current-token-SID, "SY", "BA")`, which
collapses to `("SY","BA")` for LocalSystem. So a user-token flow cannot know what to
write without an authority for the runtime principal, and hardcoding SY/BA would
break every user-mode install, where the runtime principal *is* the user.

## What Changes

- **A user-token flow that creates or recreates the state root resolves the runtime
  principal first**, and applies that principal's private DACL rather than its own
  token's default. The authority is the service account recorded in the SCM hive
  (`HKLM\SYSTEM\CurrentControlSet\Services\<name>`, whose `ObjectName` sits beside
  the `Parameters\Application` value `scripts/_service-common.ps1` already reads
  non-elevated and calls the single source of truth). With no service install, the
  runtime principal is the current token and behaviour is unchanged.
- **`doctor` gains a `state.dacl` check** that is separate from `state.placement`,
  because place and access are different questions and conflating them is what hid
  the incident. It FAILs with a named finding — never a traceback — on a root the
  runtime principal cannot open, and reports "could not evaluate" as its own state
  rather than as a pass.
- **`doctor` degrades gracefully on unreadable state**, reporting a finding instead
  of raising, across the lexical sidecar, the deferred-index backlog, and the
  rebuild-temp orphan scan.
- **CLI maintenance detects the cross-principal case up front** and refuses with a
  stable `STATE_ROOT_CROSS_PRINCIPAL` error carrying the observed descriptor, the
  required trustees, and an exact `icacls` remediation — before taking the mutation
  hold, rather than crashing mid-operation as `maintain --reconcile --rebuild-graph`
  previously did in `census_unavailable_graph_lineage`.

## Impact

A manual `exomem doctor` run now FAILs on a locally-unsafe DACL where it previously
passed, and any caller gating on its exit code stops. This is deliberate and
accepted: failing with an actionable remediation beats proceeding into the
infinite-refusal loop that caused the incident. Note `scripts/upgrade.ps1` is not
newly affected — it runs `maintain --migrate-state --offline` before its doctor
gate, and that step already raised on this condition.

Windows-only in effect; the POSIX branch no-ops and must not begin refusing.

Out of scope: recovering an *already existing* SYSTEM-owned WAL under SY/BA-only
inheritance. The user is not its owner, so `icacls /grant /t` fails on exactly that
file; only elevation or the service itself can clear it. This change prevents the
state from being created and reports it clearly when found.
