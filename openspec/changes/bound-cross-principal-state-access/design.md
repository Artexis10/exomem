## Context

The private-DACL validator is token-relative. Measured on the affected host:

```
user trustees   : ('S-1-5-21-...-1001', 'SY', 'BA')
SYSTEM trustees : ('SY', 'BA')
```

`_windows_private_dacl_trustees(sid)` returns `(sid, "SY", "BA")`, which collapses to
`("SY","BA")` when the caller is LocalSystem. So the phrase "the DACL the service
validates" has no fixed value — it is a function of the runtime account. Any fix for
requirement 1 of #933 must first answer *which principal's DACL a user-token flow
should write*, and the issue does not answer it.

## Decision: resolve the runtime principal from the service registry

A user-token flow reads the installed service's account from the SCM hive and writes
that principal's private DACL. With no service install, the runtime principal is the
current token and nothing changes.

Why this over the alternatives that were on the table:

- **Hardcode SY/BA.** Rejected. It is correct only for a LocalSystem service and
  breaks every user-mode install, where the runtime principal is the operator. It
  would trade one cross-principal failure for another.
- **Declare a two-principal root** admitting `{service account, operator, SY, BA}`.
  This is the only option that also fixes existing SYSTEM-owned children going
  forward, because they would inherit a descriptor admitting the operator. Rejected
  because it relaxes the expected trustee set, which is the invariant the private-DACL
  validator exists to enforce — the state root is meant to be private to one
  principal, and widening it to make maintenance convenient defeats that.
- **Service-mediated maintenance**, routing CLI operations through the running
  server. This is the cleanest long-term answer and the only one that can recover an
  *existing* SYSTEM-owned WAL. Rejected for this change as scope: it requires a
  maintenance endpoint and client path, making it a new capability rather than a
  repair of a shipped defect. The refusal path below makes the current behaviour safe
  in the meantime, and does not foreclose this later.

Feasibility was verified rather than assumed: a non-elevated user token *can* apply
`D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)` to a directory it owns, and can undo it, because
the owner retains `WRITE_DAC`.

The precedent for reading the SCM hive already exists in-tree:
`scripts/_service-common.ps1` reads
`HKLM\SYSTEM\CurrentControlSet\Services\<name>\Parameters\Application` non-elevated and
describes it as the single source of truth for the service's interpreter. `ObjectName`
sits beside it. A Python-side reader is new surface, and is acceptable only because
the refusal path below catches every user-token flow that gets it wrong.

## Why placement and access are separate checks

During the incident `state.placement` reported "pass — migration completed" while the
cell was completely dead. Placement asks *where* state lives; access asks *whether
this principal can open it*. Conflating them produced a doctor that was confidently
wrong precisely when it was needed. They are now two checks, and the access check is
allowed to FAIL beside a passing placement check.

## Un-evaluated is not a pass

A descriptor that could not be read, and a scan root that could not be listed, each
report their own state. `is_dir()` does not screen out an unlistable directory —
`stat` succeeds through the parent's traverse while `iterdir` is refused — so a check
that only guarded on existence reported a confident "no orphans found" derived from an
inspection that never ran. Absence must be proven before it is acted on.

## Accepted consequence: a manual doctor run can now fail where it previously passed

`state.dacl` FAILs on a locally-unsafe DACL, not only on a cross-principal one. A
manual `exomem doctor` therefore reports a failure on a root it previously passed, and
any caller gating on doctor's exit code stops.

The originally-stated version of this consequence — "upgrades that previously
proceeded will now be blocked" — was over-claimed and is corrected here.
`scripts/upgrade.ps1` runs `maintain --migrate-state --offline` (line 196) *before*
the doctor gate (line 203), and that migration already reaches
`state_paths.ensure_vault_state_dir`, which raises `WindowsRuntimeDaclError` on a
locally-unsafe DACL. So that upgrade path already stopped one step earlier, both
before and after this change. The new consequence is real for a manual doctor
invocation and for future gate callers, not for `upgrade.ps1` as currently ordered.

The trade is still the intended one: the remediation is printed with the finding, and
failing loudly beats deploying into the infinite-refusal loop.

## Open risk to settle before this ships

The trustee set is evaluated for the calling token. On a correctly-configured
LocalSystem service install, an operator running `doctor` compares expected
`{operator, SY, BA}` against an observed `{SY, BA}` and gets `unsafe` — a FAIL on a
cell that is healthy from the service's point of view. If that holds, `exomem doctor`
is red-by-construction on every Windows service install until the runtime-principal
resolution in section 4 lands, which would make the check unactionable rather than
diagnostic. This must be confirmed against a real service cell in a stop window, and
is a further argument for landing section 4 in the same change rather than deferring
it.

## Residual risk

An already-existing SYSTEM-owned WAL under SY/BA-only inheritance cannot be recovered
by a user token at all — the operator is not its owner, so `icacls /grant /t` fails on
exactly that file. Only elevation or the service itself clears it. This change stops
that state from being created and names it clearly when found; it does not repair it.
