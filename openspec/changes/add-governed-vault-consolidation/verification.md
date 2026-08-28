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

## Local failover identity stack

- Red: the first logical-id-preserving transfer failed because no authenticated
  failover identity API existed. Green: a separate owner-only target-candidate
  step mints and authenticates the fresh installation id and challenge before
  the source transfer is prepared; the target activates at generation N+1,
  preserves the logical vault id, and stale-source mutation/admission refuses.
- Red: matching source/target roots could still use an older valid export.
  Green: the verified archive manifest is projected into the canonical census
  frame and must equal both quiesced roots.
- Red: a later attachment move reset generation N to 1 and erased clone
  provenance. Green: rebind and failover preserve generation and immutable
  clone lineage while changing only the authenticated installation/root/fence
  binding.
- Red: one operation id could prepare different targets, and target bytes could
  drift after the first census but before authority reservation. Green: the
  private registry binds an operation to one target and repeats the census
  check after the source is fenced and the target is durably `DRAINING`.
- Red: crashes inside membership/control/host-registry publication could leave
  an exact transfer without a reachable recovery path. Green: the outer four
  gaps plus all three inner publication gaps recover by exact predecessor and
  target replay. Before activation the target remains non-serving; after
  fencing the old source cannot regain admission.
- Red: the signed reservation and its DRAINING membership could expire after a
  durable reservation but before the transfer journal advanced. Green: expiry
  may recover only that exact already-published reservation, authenticates its
  historical predecessor at the record's original validity time, and publishes
  a fresh current successor. A separate no-progress regression proves an
  expired acknowledgement cannot start a new reservation or change custody.
- Red: two distinct live operation ids could reserve the same source fence and
  let the second candidate adopt the first operation's attachment reservation.
  Green: the serialized private registry admits only one live transfer for an
  exact vault/installation/generation/fence tuple while preserving exact replay.
- Red: an activation successor published immediately before a crash became
  unrecoverable if that successor expired before retry. Green: exact
  uncommitted successors are historically authenticated and refreshed at the
  same epoch before control publication; exact committed successors first
  finish host-registry publication and then advance to a fresh current epoch.
  A 2x2 crash matrix interrupts both the original activation and the recovery
  after membership/control publication, then proves a later exact retry.
- Python 3.13 focused/adjoining gate: `116 passed, 1 skipped` (privileged device
  node only). Touched-file Ruff, repository `F` lint, `uv lock --check`, strict
  change validation, all OpenSpec validation (`168 passed, 0 failed`), archive
  discipline, public artifact validation (`3394 files, 3462 text payloads`),
  and `git diff --check`: green.
- Independent adversarial recheck: no P0/P1/P2 findings and GO for the explicit
  trusted-owner, same-host failover foundation after reproducing the conflicting
  live-transfer and expired-successor failures and verifying their corrections.

## Detached source-attestation stack

- Red: `tests/test_consolidation_intake.py` first failed one explicit assertion
  because the detached source-attestation subsystem was absent; collection and
  imports succeeded. Follow-on red assertions separately exposed the missing
  API, valid-proof refusal, mixed registry-generation acceptance, mutable
  verified claims, a malformed trust-set exception leak, and unbounded
  pre-parse claim admission.
- Green: the focused file passes with the exact
  `source-export-attestation/v1` JCS frame, RFC 8032 key plus fixed Exomem
  signature vector, raw unpadded-base64url Ed25519 key/signature encodings,
  closed local/Hosted claim shapes, exact source/artifact/checkpoint/fence
  binding, one private verifier purpose/audience/source registry generation,
  bounded two-key overlap, immutable verified claims, and the same content-free
  refusal at intake, apply, retirement clearance, and retirement consumption.
- This slice intentionally does not mark task 1.6 complete: the separately
  trusted control-plane-receipt alternative and its issuer record remain for a
  later bounded stack. It does not extract an archive or touch a live vault.
- Final focused/adjoining gate:
  `tests/test_consolidation_intake.py`,
  `tests/test_consolidation_cell_identity.py`, and
  `tests/test_hosted_portability.py` -> `173 passed`. Touched-file Ruff and
  format checks, repository `F` lint, `uv lock --check`, strict change
  validation, public repository artifact validation (`3396 files, 3464 text
  payloads`), and `git diff --check` are green.

## Closed portability-manifest stack

- Red: two exact restore regressions failed because the v1 parser accepted an
  unknown manifest-root field and an unknown `overall_digest` field. The first
  remained acceptable after an attacker recomputed the self-consistent digest;
  the second was outside the digest preimage entirely. Both malformed archives
  were extracted instead of refused.
- Green: v1 admission now requires the exact manifest-root and digest-object
  field sets. Both malformed forms fail with the same content-free
  `INVALID_MANIFEST` result before staging, while archive bytes and the existing
  destination parent remain byte-identical.
- This is a bounded part of task 1.8 and does not mark that task complete; its
  wider path, entry-type, resource, and runtime-state adversarial matrix remains
  explicit work. No live vault or artifact store is touched.
- Python 3.13 focused/adjoining gate:
  `tests/test_hosted_portability.py` and `tests/test_consolidation_intake.py` ->
  `133 passed`. Touched-file Ruff, repository `F` lint, `uv lock --check`, strict
  change validation, all OpenSpec validation (`168 passed, 0 failed`), public
  repository artifact validation (`3396 files, 3464 text payloads`), and
  `git diff --check` are green.

## Portable archive-metadata stack

- Red: five Windows-unsafe component forms and an unmanifested per-entry ZIP
  comment all reached manifest parsing. The path corpus included alternate-data-
  stream syntax, reserved device names, trailing dot/space aliases, and a control
  byte.
- Green: normalized archive paths now reject the closed Windows-invalid
  character/device/ending set on every host, and preflight rejects any per-entry
  comment as metadata outside the export format. Existing POSIX path, link,
  collision, compression, and resource checks remain unchanged.
- Python 3.13 focused/adjoining gate:
  `tests/test_hosted_portability.py` and `tests/test_consolidation_intake.py` ->
  `141 passed`. Touched-file Ruff, repository `F` lint, `uv lock --check`, strict
  change validation, public repository artifact validation (`3396 files, 3464
  text payloads`), and `git diff --check` are green. This is another bounded
  part of task 1.8; the full forged-runtime and resource-refusal matrix remains
  explicit and no live vault is touched.

## Portability refusal-matrix stack

- Task 1.8 is complete across this stack. The deterministic mutation corpus now
  covers supported manifest/classification versions; configured file, total,
  manifest, and path bounds; absolute, traversal, backslash, repeated-slash,
  dot-component, non-NFC, and Windows-unsafe paths; symbolic/device/hard-link
  and unmanifested-metadata entries; duplicate, case, and prefix collisions;
  payload tampering; and self-consistent forged source runtime state.
- The forged runtime corpus includes source binding, security/credential,
  lifecycle, writer lease, idempotency, restore journal, transfer temporary,
  provider log, governance, embeddings, lexical, graph, references, CLIP,
  freshness, generated frame, and voice-profile paths. Every record carries a
  recomputed file and manifest digest, so refusal proves the classification
  boundary rather than incidental corruption.
- Every archive-admission refusal in the matrix enters through
  `prepare_restore` and proves the input archive, absent destination, and
  pre-existing destination-parent bytes remain unchanged. The focused
  portability file passes `125` cases on Python 3.13. No live vault or artifact
  store is touched.
- Final Python 3.13 focused/adjoining gate:
  `tests/test_hosted_portability.py` and `tests/test_consolidation_intake.py` ->
  `168 passed`. Touched-file Ruff, repository `F` lint, `uv lock --check`, strict
  change validation, all OpenSpec validation (`168 passed, 0 failed`), public
  repository artifact validation (`3396 files, 3464 text payloads`), and
  `git diff --check` are green.
