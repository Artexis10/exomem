## Why

The epistemic loop's representation layer shipped on 2026-08-15 — predictions with `check_by`, verdicts, experiment outcomes, asserted contradictions, plan-progress review — and nothing consumes it. A prediction past its check date surfaces nowhere; an experiment that outran its stated duration is flagged nowhere; the loop-primitives change explicitly deferred these consumers to downstream review changes that were never filed. Worse, even signals that do exist reach no one: review queues run only when explicitly invoked, `write_feedback` is not among the compact terminal's projected fields, hooks exist only on the CLI clients, and hookless surfaces carry instruction prose that decays. The result measured by the no-nudge audit: the substrate detects, and the conversation never hears about it — mid-conversation accumulation (the France Farm dogfood case) is invisible until an expert user intervenes.

The delivery fix cannot be bootstrap-only (a session-start count is blind to everything that becomes due mid-conversation) and cannot assume an existing cache (the attention summary is rebuilt per call). It needs a maintained projection and registered carriers. The structural-promotion change established the exact wire posture to follow: an advisory field, validated and projected into the default compact terminal, that no client branches on.

## What Changes

- Add four due-state consumers as audit categories with attention wiring and standard fingerprint triage: overdue predictions (unit-local predicate over `check_by` and `verdict`), unfinished experiments (elapsed past declared duration with no outcome), aging unresolved questions, and supersession-integrity defects (dangling pointers, two-headed chains).
- Add a maintained due-state projection: per-family open counts with a bounded list of top item references, updated incrementally on write for the families a write can affect, re-bucketed at day boundaries so time-driven transitions surface without a write, computed per audience after egress projection so a withheld item contributes to no count on any surface, healed by reconcile, with full recompute as the fallback.
- Serve the projection on three carriers: a bounded block in the bootstrap payload; a validated advisory block on default compact mutating responses, following the established structural-suggestion posture; and a delta-only block on recall responses.
- Govern emission so the carrier cannot become its own nag: emit on change of count or first occurrence in a session, never repeat identical totals, and emit once at the end of a bulk batch rather than per write. The legacy response detail omits the block.
- Teach the contract: the bootstrap engagement guidance gains the due-state block's meaning and the instruction to read counters on every result and consult fingerprint state before re-raising anything.

## Capabilities

### Modified Capabilities

- `attention-queue`: four new signal categories with the standard review-item, fingerprint, and triage semantics, plus the maintained due-state projection contract with its invalidation, time-bucket, audience, and recovery rules.
- `command-surface`: default compact mutating responses and recall responses may carry one bounded advisory due-state block under the declared emission governance, without changing mutation identity, outcome keys, or tool input schemas.
- `agent-bootstrap-contract`: the bootstrap payload carries the due-state block and teaches its interpretation; post-write guidance names only fields the default response actually carries.

## Impact

The audit category registry, attention wiring, a new projection module with its persistence beside the review state, the compact terminal projection (one validated advisory block, mirroring the structural-suggestion path), recall response assembly, bootstrap payload and guidance text, scaffold skill regeneration through the existing packaging path, and focused tests. No MCP tool is added and no tool input schema changes, so the packaged tool-surface fingerprint and connector attestations are untouched. Write-latency gates hold: per-write projection work is bounded to the families a write can affect. Governance gains one hard obligation with its own adversarial tests: hidden items contribute nothing to any count, notice, or ordering on any surface.

Acceptance includes live thin-client evidence: the block must demonstrably survive the tool-result handling of at least one hookless web client, because the portability floor this change exists to establish is an assertion about clients, not about the server.

Deliberately out of scope: per-family suppression preferences (the later nag-governance change); any ranking effect from due state; hosted scheduled sweeps; new detectors (container health and entity recurrence are separate changes); any change to the structural-promotion suggestion.
