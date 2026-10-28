# Design

## Context

See proposal.md for the availability defect. The existing guarded scan already classifies bound originals before validating canonical receipt candidates.

## Goals / Non-Goals

The scan must preserve whole-namespace authority and companion validation. This change does not repair legacy files or infer missing identity metadata.

## Decisions

Reuse the strict frontmatter parser and existing companion classification. A readable page without frontmatter declares no receipt and becomes a skipped legacy finding.
Carry those findings through the existing capture warnings for both created and reused results. Keep malformed metadata, unreadable candidates and invalid declared receipts unavailable.
The maintained root catalog remains an expected projection without a warning. Its existing producer format recognition stays unchanged.
An unrecognized root catalog keeps its refusal because capture would otherwise rewrite unknown original bytes at that reserved path.

## Risks / Trade-offs

A former receipt with all metadata removed cannot establish its former identity. The warning exposes that limitation without blocking unrelated captures.
Whole-namespace authority already protects the warning paths. The ordinary capture warning envelope bounds their presentation.

## Migration Plan

1. Release the source repair through the ordinary runtime patch; identity captures tolerate readable legacy pages.
2. Revert the source repair if receipt validation regresses; immutable originals remain unchanged.
