# Add terminal review surface (TUI)

## Why

Exomem's epistemic depth — supersession chains, provenance, attention/health
queues, governed disclosure — is fully exposed over MCP/CLI/REST and in the
browser Review Studio, but there is no terminal-native way to *see* it. Power
users working in terminals (the product's core audience) must currently read
JSON envelopes or leave the terminal. The benchmark-foundation audit
(2026-07-31) confirmed no TUI change exists, active or archived, and produced
a requirements handoff (`docs/tui-requirements-handoff.md`) grounded in the
shipped review surfaces (`review_memory`'s 13 modes, `review_item_context`,
`triage_memory`), the Review Studio's security model, and a UX audit of Gray
Box's minimal hand-rolled terminal UI. This change is authored by the
benchmark-foundation session as a handoff; a separate session/worktree
implements it. The benchmark worktree will not modify production TUI files.

## What Changes

- A terminal review surface launched from the existing CLI (zero-argument
  full-screen entry; every subcommand remains scriptable), rendering: the
  epistemic inbox and health queues with fingerprint-bound triage; a
  single-item workspace (body, evidence/provenance, graph neighbours,
  history, evolution) from `review_item_context`; supersession/evolution
  chains with current-vs-superseded state always distinguished; trust
  signals the product actually has (citations, recency, contradiction flags
  — never invented scores); governance visibility (per-audience disclosure
  levels, withhold notices, recent receipts) read-only; and
  automatic-retrieval status (hook install state, last nudge/injection,
  cooldowns).
- All data flows through the existing product-command registry (`--json`
  envelopes); the TUI is a renderer and MUST NOT recompute rank, severity,
  or epistemic meaning.
- Writes limited to triage, relation-accept, and pack selection via their
  existing governed commands; destructive/structured operations stay
  CLI-only with the exact command surfaced.
- Optional journey replay hooks so Track D benchmark journeys double as
  inspectable demo flows.

## Capabilities

### New Capabilities
- `terminal-review-surface`: terminal-native review, evolution, provenance,
  governance-visibility, and retrieval-status rendering over existing
  product commands.

### Modified Capabilities
None.

## Impact

- New CLI-only surface; no MCP/REST surface change, no tool-surface digest
  change, no server behaviour change.
- Pure substrate: the TUI process makes no model calls; it measures and
  renders.
- Dependency posture: stdlib-first is proven viable (Gray Box precedent); any
  TUI framework addition is an explicit decision inside this change's
  implementation, default none.
- Renders through egress-governed commands only, so governance projections
  and scrubbing apply unchanged; no vault content lands in logs or static
  assets (Review Studio inert-shell principle).
- Requirements source of truth: `docs/tui-requirements-handoff.md`.
