# Governance hardening verification ledger

This ledger records durable merge and verification evidence for completed waves of
`harden-governance-for-consolidation`. It is bookkeeping only: it does not claim that
the remaining hardening sections or governed consolidation are complete.

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
operations after all hardening and consolidation implementation evidence is complete.
