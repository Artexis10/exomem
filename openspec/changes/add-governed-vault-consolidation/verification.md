# Governed vault consolidation verification ledger

This ledger is append-only evidence for the active change. Product implementation
and tests use generated temporary vaults only. It is not rehearsal, cutover, or
source-retirement approval for any real operator vault.

## Prerequisite gate

- Base revision: `32170d098970a409fd3305fe8f59d302439ebed3`.
- The prerequisite change is archived at
  `openspec/changes/archive/2026-08-28-harden-governance-for-consolidation/`;
  therefore its old active change name is intentionally no longer accepted by
  `openspec validate <name>`.
- Its archived `verification.md` records umbrella PR `#900`, merged as
  `7c5a315797340d89c07d74de6d61de9a1c0c3c9c`, exact-head CI run
  `33137995631` with 19 successes, 8 intentional skips, and no failure, Hosted
  run `33137995634` green, wire run `33048555592` with 58/58 green, independent
  security review/recheck GO, independent verifier GO, canonical sync, and no
  real-vault execution.
- Fresh replacement validation on this merged base:
  `openspec validate --all --strict` -> `168 passed, 0 failed`.

## Immutable pre-change surface

`pre-consolidation-baseline.json` freezes the MCP fixture, tool/client contract,
capability document, OpenAPI operations, bootstrap byte sizes and ordering,
Hosted v1-v4 command memberships and descriptor hashes, generated plugin and
manifest trees, deployment locks, and registered promotion evidence. Later v5
work must reproduce every v1-v4/core hash and must add v5 only through explicit
selection.

## Red-first protocol

For each implementation slice below, record the focused test command and the
missing-invariant assertion before product code, then the exact green result
after the smallest production change. Collection/import failures do not count.

## Real-vault exclusion

All commands in this ledger must use pytest temporary paths or explicitly
generated fixtures. The personal and POLLY vaults are out of scope. Capability
completion does not authorize rehearsal, cutover, or retirement.

## Identity/adoption evidence

### Dedicated identity subsystem

- Red: `uv run --frozen python -m pytest -q tests/test_consolidation_cell_identity.py`
  -> `1 failed`; the assertion reported that
  `exomem.governance.consolidation_identity` did not exist. Collection and
  imports succeeded.
- Green: the same focused command -> `1 passed` after adding the explicit
  private subsystem boundary.

### Local and Hosted identity/adoption

- Red: focused identity run -> `4 failed, 1 passed`; the four assertions named
  the missing local adoption/load and Hosted adoption APIs.
- Green: focused identity run -> `5 passed` after the smallest authenticated
  record/adoption implementation.
- Red: reused-installation regression -> `1 failed`; two independently
  provisioned roots accepted the same generated installation id.
- Green: the identity registry now serializes through one host-wide mutation
  boundary, verifies every retained owner-only claim, and refuses duplicate
  installation or root binding.
- Red: Hosted alias regression -> `1 failed`; `HostedBindingV2` admitted equal
  routing `cell_id` and logical `vault_id`.
- Green: the binding constructor now refuses the alias before root/readiness or
  owner-context work.
- Red: Hosted explicit-load regression -> `1 failed`; the private record could
  only be reached through the provisioning operation.
- Green: Hosted loading is explicit, verifies the trusted binding/root and an
  accepted cell key, and never provisions a missing record.
- Final focused/adjoining gate:
  `tests/test_consolidation_cell_identity.py`,
  `tests/test_authorization_standalone_provisioning.py`, and
  `tests/test_hosted_binding_v2.py` -> `94 passed, 1 skipped` (privileged device
  node only). The identity file tests cover closed schema/version, malformed
  authentication, forged fence, missing machine key, wrong mode, symlink,
  hard-link, copied local/Hosted roots, idempotent replay, ordinary post-adoption
  content drift, pre-commit failure, and lost-ack recovery.
- Red: owner-authorized move/rebind regression -> `1 failed`; the existing
  attachment transfer correctly fenced the old root, but consolidation identity
  had no operation that could bind the new stable root while preserving logical
  and installation ids.
- Green: the rebind operation composes the existing drained attachment-transfer
  proof, verifies both stable roots and the target host registry, CAS-replaces
  only the authenticated root/fence commitment, preserves immutable adoption
  provenance and both ids, and replays idempotently.

## First-stack acceptance

- Python 3.13 focused/adjoining gate: `94 passed, 1 skipped`.
- Touched-file Ruff: green. Repository-wide required `ruff --select F`: green.
- `uv lock --check`: green.
- `openspec validate add-governed-vault-consolidation --strict`: green.
- `openspec validate --all --strict`: `168 passed, 0 failed`.
- OpenSpec archive discipline: green.
- Public repository artifact validation: `3394 files, 3462 text payloads`, green.
- `git diff --check`: green.

## Rehearsal-clone stack

- Red: explicit rehearsal-clone regression -> `1 failed`; no product operation
  could derive fresh active identity plus authenticated clone lineage without
  accepting those ids from input.
- Green: the same regression passes after adding an owner-only clone operation
  that discovers the source through the machine-authenticated root registry,
  requires byte-identical canonical source/clone snapshots, and generates fresh
  logical and installation ids for the target.
- Focused identity gate: `23 passed`. It also proves snapshot drift and copied
  identity bindings are refused and two clones share lineage evidence without
  sharing either active id.
- Python 3.13 focused/adjoining gate: `97 passed, 1 skipped` (privileged device
  node only).
- Touched-file Ruff, `uv lock --check`, strict change validation, public
  repository artifact validation (`3394 files, 3462 text payloads`), and
  `git diff --check`: green.
