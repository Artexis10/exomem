## Context

Two different questions were being answered by one function.

`held_fs.probe(root)` asks whether a *particular filesystem* can support
mount-aware, descriptor-relative, no-follow operations. It is cached per root
and can legitimately differ between two vaults on one host.

`_held_fs_posix._probe` answers a second question first: whether this
*platform* has an implementation at all. That answer can never differ between
vaults and can never be repaired by choosing a different one — but it was
returned in the same `Capabilities.disabled(...)` shape, so it reached callers
as `CAPABILITY_UNAVAILABLE`, and `reserved_paths` turned it into
"could not acquire the vault". A build fact arrived dressed as a filesystem
fact, once per operation.

## Goals / Non-Goals

**Goals.** One refusal that names the platform. Diagnosis reachable on the
platform that cannot run. Distribution metadata and documentation that match
what the code does.

**Non-Goals.** A darwin backend. Any weakening of the substrate's contract. Any
change to what Linux or Windows do — every predicate added here is true on both,
so both take exactly the path they take today.

## Decisions

**The predicate is root-free and lives beside the backend.** `platform_support()`
delegates to the selected backend's `platform_supported()`, so the Windows
backend answers `True` by construction — it is only imported on Windows — and
the POSIX backend keeps the Linux rule next to the probe that already depended
on it. `_probe` now calls that same function, so there is one platform rule
rather than a copy that can drift; a test pins that patching the predicate
changes what `_probe` returns.

**Refuse in the CLI, never at import.** Import-time refusal would break the
library for anything embedding it and would break the test suite before it could
report anything. The refusal sits in `_run_cli`, after `--version` and before any
vault work.

**The allowlist is diagnosis and identity, not a general escape.** `doctor` and
`install-info` run; a bare flag (`--help`, `--version`) runs. Everything else
refuses. There is no remote-client mode to protect: the CLI has no path that
operates against a remote server instead of a local vault, so every other
command genuinely needs the substrate.

**Doctor fails rather than warns.** A warning invites a user to proceed. Nothing
downstream of an absent backend works, so the honest severity is the one that
stops.

**The suite skip reuses the existing hook.** `tests/conftest.py` already converts
five absent-capability refusals into skips, and its docstring records doing this
for macOS before — "a missing capability is what `skip` means". Adding a sixth
branch is consistent with that, and it is gated on the capability actually being
absent, so on Linux and Windows CI these refusals stay failures.

**The matcher refuses to match the cascade.** The same file's rule is that a
digest mismatch stays a failure "because that is a real finding". The graph-epoch
incoherence that follows an unpublishable write is exactly such a finding when it
happens for any other reason, so the matcher is anchored on the acquisition
sentences only. This is a deliberate choice to leave some macOS failures red
rather than risk masking a lineage defect.

**The launchd recipe is labelled, not deleted.** It describes the shape a served
macOS would use. Deleting it would cost that work and gain nothing; leaving it
unlabelled was the defect.

## Risks / Trade-offs

**A user who wants macOS gets a refusal instead of a workaround.** That is the
point, and it is better than the current outcome, which is a vault that appears
to accept a write and a substrate that refuses underneath it.

**The disclosure could be read as dropping macOS support.** It is not a decision
to drop it — the classifier never described a working platform, and the spec
delta says MPS/MLX guidance returns alongside a backend.

**Unverifiable residue.** The share of a macOS shard that stays red after the
skip branch cannot be measured from Linux. Stated in the proposal rather than
guessed at.
