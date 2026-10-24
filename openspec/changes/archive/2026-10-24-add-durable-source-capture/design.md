# Design

## Context

The mutation engine already persists scoped replay results with bounded retention.
Source capture allocates a fresh path on each execution; artifact adoption already demonstrates canonical receipt lookup under a mutation guard.
See proposal.md for the reconciliation problem.

## Goals / Non-Goals

The design adds portable capture identity without another journal, engine or cache.
It excludes content deduplication, legacy backfill, revision ordering and attachment identity changes.
No model runs.

## Decisions

Store the typed identity and original capture digest in the initial atomic Source write.
Use one schema and one digest function; preserve text exactly and normalize only the existing capture fields' established normalization.
Hash the requested slug before path allocation, so collisions and dates do not change caller intent.
Source kind resolves through the open registry, including the existing alias.

Scan canonical Sources and Evidence inside the existing mutation guard before calling the existing capture writer.
Use strict frontmatter parsing and guarded reads; a failed enumeration or candidate read returns unavailable rather than treating missing evidence as absence.
Derived indexes cannot establish uniqueness, and a second receipt database would create another recovery obligation.

Use the existing raw-role registry and byte-binding helpers to distinguish canonical pages from Markdown originals.
Require a producer-defined companion path as well as an exact byte binding before adding an ownership edge.
Resolve verified companion-to-original edges from nodes without incoming edges, then discard originals' outgoing declarations.
Retain parse and binding failures until this classification finishes, so an original's frontmatter cannot impersonate a receipt or suppress another page.
Rootless cycles remain unavailable. Explicit promotion preserves recovery through the same identity and reference.

Require existing unrestricted content access before scanning and the existing creation boundary before returning either created or reused.
Recheck content authority in atomic-write validation.
Run the same authority check in shared dispatcher preflight before saved invocation replay or key mismatch checks.
This prevents hidden-identity probing; a false refusal withholds identity capture from a restricted caller but leaves ordinary capture available.

Consume explicit replay keys from namespaced `tools/call` metadata at the MCP adapter and pass verified normalized principal scope to the existing retry engine.
This preserves discovery byte budgets without repeating a transport field in every tool schema.
Preserve unkeyed behavior, verified client and vault namespaces, mismatch errors and issuance exclusions.

## Risks / Trade-offs

- Canonical scans cost linear time in the small Source corpus; this avoids making delayed sidecars uniqueness truth.
- A damaged Source blocks identity capture until repaired, because incomplete enumeration cannot safely prove absence.
- A manually renamed legacy artifact pair can withhold identity capture until its canonical pairing is repaired; ordinary capture remains available.
- New public schemas consume discovery bytes; use maintained generators and keep the existing budgets.

## Migration Plan

Existing Sources need no migration. New identity captures carry their own metadata from the first write.
Rollback removes the optional entrypoint while preserving canonical metadata and existing Sources.
