# Tasks

## 1. Design finalization (implementing session)
- [ ] 1.1 Confirm rendering stack decision (stdlib ANSI vs dependency) with an explicit dependency-posture note in this change
- [ ] 1.2 Screen inventory + keymap derived from `docs/tui-requirements-handoff.md` (queue list, item workspace, evolution inspector, retrieval-status panel, pack picker)

## 2. Implementation
- [ ] 2.1 Full-screen entry from the existing CLI (zero-argument launch; subcommands unchanged); terminal robustness contract (cursor restore, interrupt-safe loop, persistent context banner)
- [ ] 2.2 Queue views over `review_memory` modes with fingerprint-bound triage via `triage_memory`
- [ ] 2.3 Item workspace over `review_item_context` (body, provenance/evidence, graph, history, evolution)
- [ ] 2.4 Evolution inspector (supersession chain, current-vs-superseded marking, transition rationale)
- [ ] 2.5 Retrieval-status panel (hook install state, last nudge/injection, cooldowns)
- [ ] 2.6 Governance visibility (read-only disclosure levels, withhold notices, recent receipts)
- [ ] 2.7 Relation-accept + pack-selection writes through existing commands; destructive ops surfaced as CLI commands only

## 3. Verification
- [ ] 3.1 Unit tests for view-model construction from recorded command envelopes (no live vault needed)
- [ ] 3.2 Terminal robustness tests (exit paths restore terminal; interrupt cancels action, not app)
- [ ] 3.3 Assertion tests: no numeric confidence anywhere; superseded never rendered as current; no direct vault file reads in the TUI module
- [ ] 3.4 `openspec validate add-terminal-review-surface --strict`; lean suite green; tool-surface digest unchanged (`git diff --exit-code tests/fixtures/mcp_tool_schemas.json`)
