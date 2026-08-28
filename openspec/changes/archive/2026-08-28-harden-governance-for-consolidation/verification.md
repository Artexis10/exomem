# Governance hardening verification ledger

This ledger records durable merge and verification evidence for
`harden-governance-for-consolidation`. It closes the prerequisite hardening only; it does
not claim that governed consolidation itself has shipped.

## Per-scope disclosure lattice

The disclosure-kernel wave shipped as PR #464, `fix(governance): close disclosure
crossover`, merged on 2026-08-13 as `1a02d9594019669b075c3a02bbeaa61be1d8d0da`.
Its reviewed head was `68f064669893a43e83b14368d91bf449606a304b`.

The implementation evidence attached to that exact head records:

- the initial red packet at 10 failures and 461 passes, covering sibling-scope grant
  crossover, equal-level purpose widening, unsafe option acceptance, and disableable
  terminal scrubbing;
- subsequent red-first correction packets for option-meet associativity, release-field
  identity, canonical bearer parsing, collision-safe mapping-key scrubbing, strict YAML
  maps, and owner-only scope explanations;
- same-reviewer recheck with a GO verdict and no remaining findings;
- an independent verifier result of 553 packet tests, 831 adjacent governance tests,
  and 32 targeted security probes; and
- a fresh 553-test packet pass after rebasing on the then-current `main`, changed-file
  Ruff, and `git diff --check`.

The PR CI result is all-green: eight Python shards, installed product E2E, retrieval
quality and latency, 2k/8k semantic-write latency, OpenSpec validation, packaging,
lint/types, and the required combined gate all passed. The shipped scope is deliberately
bounded: schema-v3 session grants are inert because they cannot prove reviewed scope
identity; exact persisted scope proofs and migration remain tasks 1.7 and 1.10.

This evidence closes tasks 1.1-1.6, 1.8-1.9, and 1.11 only.

## Reserved administration path monopoly

The held-filesystem and reserved-path wave shipped as PR #717,
`fix(governance): reserve internal state paths`, merged on 2026-08-21 as
`6587ad8c7b9f5282d196d0273f393ed5dc7c7161`.

Its exact merged head passed the full Linux/Python matrix, installed product E2E,
retrieval quality and latency, graph convergence, 2k/8k semantic-write latency,
OpenSpec validation, packaging, lint/types, and the combined required gate. Native
Windows/NTFS verification passed in 3m11s with relative-handle, no-follow/reparse,
8.3-alias, rename/disposition, and fallback-disable coverage. The broad Windows shards
also passed at 27m58s, 28m19s, 28m18s, and 34m33s.

The final Windows result includes the performance correction for high-frequency
reserved-identity coordination: synthetic internal locks retain their OS exclusion and
per-owner ordering but omit diagnostic holder-sidecar fsyncs. The formerly timing-out
shard 3 completed in 28m18s under the unchanged 45-minute session timeout.

PR #717 landed both the native primitive/internal-identity-publication work and the
registry/dispatcher/leaf/surface enforcement before this ledger marked the combined
gate complete. This evidence closes task 4.11.

## Operational boundary

Neither shipped wave invoked consolidation, migrated a real vault, or accessed a live
vault. Clone rehearsal, real cutover, and source retirement remain separately gated
operations after the consolidation command itself is implemented and verified.

## Consolidation-prerequisite integration

The remaining implementation shipped through umbrella PR #900,
`feat(governance): harden governed consolidation`, merged on 2026-08-28 as
`7c5a315797340d89c07d74de6d61de9a1c0c3c9c`. The release-facing head was
`a1540feeb16832914cc7edbe64dfe700a851f3c7`; PRs #832-#899 retain the bounded
stacked review trail and exact red/green commands for their individual slices.

The integrated implementation includes exact schema-v4 policy/projector/catalog
activation, bearer-free authorization-session state, protected external custody and
serving-membership epochs, v3/v4 migration and downmigration, semantic
suspend/resume/undo, projected retrieval and continuation closure, native held-file
enforcement, standalone attachment move/restore/clone fencing, Hosted custody
publication, generated surface parity, overlap/migration fixtures, and generic operator
documentation.

Independent adversarial reviews found and closed the following load-bearing clusters:

- projected retrieval initially retained raw/post-filter acquisition, shared grant
  crossover, raw annotation reopens, non-applicable CLIP/graph warming oracles, inert
  graph votes, reducer/rerank divergence, and deadline self-waivers; the final recheck
  found no remaining P0/P1/P2 issue in `tests/test_governance_projection_*`,
  `tests/test_governance_oracle_closure.py`, and the authorization-session suites;
- held-file review found Windows reparse/relative-operation, delete-before-directory-
  flush, flush-handle access, parent-exchange classification, and live graph WAL lock
  ordering defects; each received a red regression in
  `tests/test_reserved_admin_paths.py`, `tests/test_windows_path_alias_guard.py`, or
  `tests/test_mutation_concurrency.py`, and the same reviewer returned GO;
- migration review reproduced a critical restore race that could discard a committed v4
  session while preserving its receipt; the corrected exclusive store/receipt protocol
  passed 47 focused tests and the same reviewer returned GO;
- semantic-operation review found pending-YAML piggybacking, incomplete undo of added
  files, stale catalog memberships, sweep/GC recovery loss, direction misclassification,
  and historical replay coupling; retained generation history, rebuilt catalog targets,
  strict manifests, and terminal replay closure passed focused and full gates, with a
  final GO;
- attachment review reproduced stale-source revival after custody transfer; the fixed,
  independently keyed host-control attachment root and transfer fencing passed 48
  focused tests and the final review returned GO; and
- carrier/session review closed pre-FastMCP validation ordering, bearer-copy scrubbing,
  cross-cell identity, key membership, generated-envelope, and Windows custody issues;
  no accepted finding remained open at integration.

The final exact-head evidence is:

- [CI run 33137995631](https://github.com/Artexis10/exomem/actions/runs/33137995631):
  19 successful jobs, eight intentional non-applicable skips, zero failures, including
  all Python 3.13 core/harness shards, native Windows NTFS held-file coverage,
  installed-wheel stdio/HTTP E2E, package/public-artifact checks, OpenSpec, Ruff/types,
  onboarding, TUI, and the required combined gate;
- [Hosted run 33137995634](https://github.com/Artexis10/exomem/actions/runs/33137995634):
  the complete Terraform/TFLint/Checkov/Ansible/Helm/policy/SOPS/type/test/secret-scan
  static job passed, including 520 provisioner tests with the real runtime custody
  verifier available only as a test dependency;
- [wire release run 33048555592](https://github.com/Artexis10/exomem/actions/runs/33048555592):
  all 58 jobs passed, including the 12-route no-model and 12-route
  `vectors-cpu-torch-v1` 200-pair actual-wire matrices at declared capacity; and
- the one current-main merge conflict retained fail-closed
  `MembershipUnresolved` handling in `governance/egress.py`; 13 directly affected
  membership/snapshot tests passed before the exact-head CI run.

The public registry still has no `consolidate_memory` command. `_Consolidation` remains
reserved with an explicit no-owner refusal until that later command ships. No real vault,
POLLY cell, or personal cell was opened, migrated, combined, or otherwise mutated by this
work.

## Canonical sync and archive closeout

After the implementation and review evidence above was complete, OpenSpec archived the
change as `2026-08-28-harden-governance-for-consolidation` and synchronized all six delta
specs into the canonical capability set. The sync added the canonical
`authorization-session-binding` specification and updated `command-surface`,
`get-payload-shape`, `governance-authoring`, `governance-kernel`, and `release-gate`.
OpenSpec reported 13 added requirements, 26 modified requirements, zero removals, and zero
renames.

The post-sync/archive command `openspec validate --all --strict` passed all 168 active
changes and canonical specifications with zero failures. The implementation revision
remains the #900 squash merge `7c5a315797340d89c07d74de6d61de9a1c0c3c9c`; the review,
wire, Hosted, and exact-head CI records are the immutable runs linked above. This archive
closes the governance hardening prerequisite only. It does not expose consolidation and
does not authorize rehearsal, cutover, source retirement, or mutation of any live vault.
