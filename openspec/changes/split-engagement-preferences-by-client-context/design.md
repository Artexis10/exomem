## Context

See proposal.md. The shipped 0.81.0 capability stores one record per vault and verified principal, `{"schema": 1, "prominence", "change_id"}`, under exact-revision compare-and-swap, and resolves effective prominence as operator environment, then the saved value, then legacy machine configuration, then the detected client default. Surface detection already distinguishes known hookless clients (ChatGPT, claude.ai, hosted and web surfaces) from coding clients (Codex, Claude Code) and unknown clients.

## Goals / Non-Goals

Goals: one identity can hold Balanced for coding clients and Maximal for conversational clients at the same time; a change made through one client is visible to every client of the same identity and vault on its next request; existing 0.81.0 clients keep working unchanged.

Non-goals: per-project profiles, temporary focus modes, separate recall, capture and narration knobs, nudge frequency, a new hosted agent profile, account linking between credentials. Those are discussed separately and are not built here.

## Decisions

### Two contexts derived from the detected surface

`coding` and `conversation` are the only contexts. A surface in the known hookless set resolves to `conversation`; Codex, Claude Code and unknown or undetected clients resolve to `coding`, which preserves today's Balanced default for unknown clients. Context comes only from the surface the server detects or from the operator's explicit surface override; a request argument never supplies it. Client names remain untrusted hints that tune eagerness and cannot select identity, vault, storage or authority.

### One record, one revision, schema 2

The record becomes `{"schema": 2, "prominence": level or null, "contexts": {context: level}, "change_id"}` in the same per-principal file. One revision covers the whole record, so a stale write to one context cannot race a write to another and `set` and `clear` cannot interleave under one inspected revision. A schema-1 record reads as its level with no contexts; the first write rewrites it as schema 2. Unknown context keys, invalid levels, an empty record and every existing malformed shape fail closed as before.

The one-way upgrade is safe for the 0.81.0 reader: its strict shape check raises the unavailable error, the request snapshot records `unavailable` and resolution degrades to machine configuration or the surface default; its writer re-reads before writing and raises the same error, so it never erases a schema-2 record.

### Context tier in precedence

Effective prominence resolves from a valid operator environment override, then the saved value for the request's context, then the saved identity-wide value, then legacy machine configuration, then the surface default. The resolved payload reports the context and a distinct `preference:context` source so bootstrap, workflow capture and the envelope can show which value applied. A conflicting set or clear under an operator override is refused before writing, as today.

### Canonical operation

`configure_memory(action="inspect"|"set"|"clear", prominence=None, expected_revision=None, context=None)`. Inspect is read-only and reports the identity-wide value, the per-context map, the request's detected context and the effective level. `set` without `context` keeps its 0.81.0 meaning and writes the identity-wide value; with `context` it writes that context. `clear` requires `context` and `expected_revision` and removes one context value; clearing an absent value is a no-op that reports no mutation. Parameters derive from the function signature, so MCP, REST and CLI schemas, the pinned schema fixture, the tool-surface contract, the plugin tree, the hosted renders and the capabilities document are regenerated rather than edited.

### Hosted profiles unchanged

`configure_memory` stays excluded from the versioned hosted agent profiles. The personal service already exposes the full canonical surface to conversational connectors, so the split works there without a hosted change.

## Risks / Trade-offs

- An older service reading a schema-2 record silently loses the saved choice until upgraded → acceptable for a one-way upgrade of a preference; inspect reports the unavailable state rather than a fabricated value.
- A client misclassified as conversational would receive the wrong context → the mapping is pinned by tests for every known client name and for unknown names, and the operator surface override remains authoritative.
- Context could leak into identity or authorization code → the reviewer checks every path where the context string travels; it must reach only prominence resolution and the preference record.
- Derived artifacts drift after the signature change → regeneration is an acceptance step, not a follow-up.

## Migration Plan

Deploy the additive resolver, storage and command changes. No live preference is rewritten by deployment; records upgrade on their next successful write. Rollback leaves schema-2 records in place and the previous reader degrades to configuration or defaults without deleting them.
