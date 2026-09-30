## Why

Binary Evidence preservation over the ChatGPT connector has been producing ambiguous outcomes since at least 2026-08-30, and the call ledger on the serving host shows why. Every `MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN` on `preserve_artifacts` (2026-08-30, 2026-08-31, four on 2026-09-09) carries post-commit spans of `index.upsert_after_write` (1 to 15 s) and `graph.refresh_paths`: the bytes were committed, then the derived-state acknowledgement failed, and because the terminal is persisted only after that acknowledgement, no terminal existed for the identity. The client's same-identity retry ten seconds later (identical `request_bytes`) therefore resolved to the fail-closed `MUTATION_OUTCOME_UNKNOWN`, exactly as the mutation-terminal contract specifies today. The agent was left with "committed but unknown", searched the vault by hand, and the conversation stalled.

Two neighbouring defects compound it. The plain `preserve_artifacts` path has no content-hash idempotency (only the `adoption` lane checks a receipt before writing), so the natural recovery, retrying under a new identity, duplicates append-only Evidence. And `preserve.py:_sanitize_segment` deletes path separators from `scope` and `category` instead of refusing them: the ledger's argument shapes for the 2026-09-09 19:22Z and 21:45Z calls match `food/caviarhouse-group-order` exactly, and the vault now holds one logical case split across `Evidence/food/caviarhouse-group-order/` and `Evidence/foodcaviarhouse-group-order/`.

The hosted response shape table omits `MUTATION_OUTCOME_UNKNOWN`, so the ChatGPT client does not even receive the structured `status`/`committed` fields for the one error it most needs to interpret, and ledger rows for artifact writes carry `target_paths: []`, so the ledger cannot say what landed.

## What Changes

- A committed mutation's canonical terminal is persisted before derived-state acknowledgement runs. Acknowledgement failure degrades the success terminal to `derived_sync: "pending"` with a warning; it no longer produces a committed-uncertain error, and a same-identity retry replays the persisted terminal. Committed-uncertain remains for the case the contract already names: canonical commit proven, terminal persistence itself failed.
- `preserve_artifacts` and `preserve_evidence` return one terminal state per file (`stored`, `already_stored`, `failed`) plus the batch's `request_id` and `receipt_id`, and the per-file record carries `path`, `ref`, `hash`, `hash_algorithm`, `size`, `media_id`.
- Content-hash idempotency on the plain preservation path: a staged artifact whose sha256 already exists under the same `Evidence/<scope>/<category>/` destination is reported `already_stored` with the existing path and ref, and nothing is written.
- `scope` and `category` are validated as single path segments. A separator or reserved character is refused with `INVALID_PRESERVE`, the offending field named, before any byte is staged; nothing is silently normalised.
- The hosted error-shape table gains `MUTATION_OUTCOME_UNKNOWN` (`status="uncertain"`, `committed=null`) so the ChatGPT client receives the structured fields.
- Ledger rows for artifact and evidence writes record the committed relative vault paths in `target_paths` (structural paths only, never content).
- The scaffold reference `mutation-results.md` and the plugin copy explain the three per-file states and the retry rule: retry the same identity to recover a lost acknowledgement; a duplicate under a new identity is reported, not stored twice.

## Non-goals

- The connector latency budget (recall calls of 1 to 15 minutes, artifact writes of 14 to 95 s) and deferring media extraction and index fan-out out of the request path. That is the next change; this one only stops latency-induced acknowledgement failures from becoming ambiguous outcomes.
- The stale ChatGPT action schema (`deploy/chatgpt/personal-plugin-contract.json`, registered at 0.45.0, pending digest awaiting post-deploy acceptance). That is an operator step, not code.
- Bridging ChatGPT attachment ids to fetchable handles; the connector supplies `download_url` today and this change does not alter the handle contract.
- Structural promotion and transient-state hygiene sensors from the same dogfood report.

## Capabilities

### Modified Capabilities

- `mutation-terminal-contract`: terminal persistence precedes derived acknowledgement; committed-uncertain is narrowed to terminal-persistence failure.
- `client-artifact-preservation`: per-file terminal states, content-hash idempotency, destination-segment refusal.
- `retry-safe-mutations`: a same-identity retry after a lost acknowledgement resolves the persisted terminal for artifact batches.
- `hosted-mutation-safety`: hosted error shapes cover `MUTATION_OUTCOME_UNKNOWN`.
- `call-ledger`: artifact and evidence writes record committed target paths structurally.

## Impact

- Code: `src/exomem/writer_lease.py` (terminal persistence order, derived acknowledgement degradation), `src/exomem/client_artifacts.py` and `src/exomem/preserve.py` (per-file states, hash lookup, segment refusal), `src/exomem/server_hosted.py` (shape table), `src/exomem/call_ledger.py` and the command wrappers (outcome target paths), `src/exomem/_scaffold/_Schema/references/mutation-results.md` with the plugin copy and regenerated derived artifacts (plugin tree, hosted renders, v5 candidate, `docs/capabilities.md`, tool-surface fingerprint).
- Tests: fault-injection on derived acknowledgement, replay after lost acknowledgement, duplicate bytes under a new identity, separator refusal with zero writes, hosted shape, ledger target paths, mixed-batch per-file states.
- Live vault: the split `foodcaviarhouse-group-order` tree is operator repair after deploy (move the two files under the canonical family through governed tooling), recorded for closure only.
