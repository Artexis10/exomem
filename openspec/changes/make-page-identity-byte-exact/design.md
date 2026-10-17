# Design

## Context

See the proposal for the failure mechanism. Guarded byte reads and the existing UTF-8 text hash already provide the required primitives.

## Decisions

Read exact bytes once and decode without universal-newline translation. Reuse guarded readers where publication depends on source stability. Keep intentional normalized semantic hashes unchanged and document their different purpose.

Audit text-read-to-hash call sites across the package. Repair the readers and their comparisons together. Identity backfill preserves the source's newline representation when inserting its field.

The closest maintained solution is the existing guarded-read and atomic-write layer. Extending its use avoids another hash library, migration framework, or normalization pass.

## Risks / Trade-offs

Legacy derived hashes can be stale until normal refresh. Keep existing proof and recovery behavior; never accept an unproven row or rewrite canonical data to match it.

## Migration Plan

Deploy the repaired readers through the ordinary package release. Use existing derived recovery when needed. Retain the previous installed release for operational rollback; do not downgrade derived schemas or rewrite pages.
