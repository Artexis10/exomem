# Design — terminal review surface (handoff)

This change is a requirements handoff; the implementing session owns detailed
design. Binding inputs:

- `docs/tui-requirements-handoff.md` — the requirements source of truth:
  what must be visible (current truth vs history, evidence/provenance,
  attention/health queues, honest trust signals, governance visibility,
  retrieval status), the twelve adopted Gray Box UX patterns, and v1 scope
  candidates.
- Shipped surfaces to drive, not reimplement: `review_memory` (13 modes),
  `review_item_context` (bounded item workspace payload), `triage_memory`
  (fingerprint-bound), `connect_memory(accept-relation)`, pack selection.
- Review Studio security model transfers: inert shell, all data via governed
  commands, client filters but never recomputes.
- Implementation freedom: stdlib ANSI + readchar-class minimalism is proven
  viable (Gray Box, ~300 lines); Rich/Textual is permitted only as an
  explicit dependency decision inside the implementing change.
- Parallel-work boundary (2026-07-31): the benchmark-foundation worktree
  owns benchmark/corpus/adapters/scorers/harnesses/eval docs and will not
  modify production TUI files; the TUI worktree branches from the
  Milestone-1 commit and owns this change end to end.
