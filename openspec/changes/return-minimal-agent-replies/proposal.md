## Why

An agent that calls Exomem reads every key of every reply. Today a committed write returns about 1.1 KB of operational state (`ok`, `state`, `terminal`, `status`, `mutated`, operation and receipt identities, hashes, `*_sync*` fields, `warnings_count`, a vocabulary delta and a due-state block). A search returns hits that name each page twice and a `warming` block. A refusal returns a duplicated envelope with remediation prose and internal ids. None of this feeds the agent's next call or what it tells the user. It fills the agent's context, and the useful facts sit among keys that mean nothing to it.

## What Changes

The decision: **a default reply carries only what the agent tells the user and what its next call needs. Everything operational moves behind `response_detail=full`. A refusal names what happened to the data and the exact retry.**

- A write reply leads with `user_summary` and carries the result fields a later call takes, plus `next_action` (one sentence, only when the agent must act) and `notice` (one sentence, only when the outcome is partial, unknown or stale). The operational fields move to `diagnostics.operational`, shown only with `response_detail=full`.
- A write reply no longer carries `due_state` or `vocabulary_delta`. A search reply no longer carries `due_state`, a `warming` block or a `scope` label, and names each page once.
- The capture-sweep rule text (about 750 bytes) becomes one `next_action` sentence, sent once per episode.
- An MCP refusal becomes `{"error": {"code", "message", "retry"?, "candidates"?}}`. `message` is one sentence that ends with what happened to the data. `retry` is the exact next call. The old envelope moves to `diagnostics` under `response_detail=full`.
- Bootstrap guidance for post-write replies teaches `user_summary`, `next_action` and `notice`, and names no sync field.
- Tool names, request schemas, tool descriptions, write governance and the `recall_context` packet do not change. The CLI and REST facades keep their `{success, data}` wrapper and their public error block.

## Impact

- Affected specs: `mutation-terminal-contract`, `command-surface`, `agent-bootstrap-contract`, `category-retrieval-reliability`, `attention-queue`, `context-activation`.
- Affected code: the mutation terminal, the MCP refusal seam in `command_surface`, the search command in `commands`, the bootstrap guidance text, and the scaffold references that describe replies.
- Programs that read operational keys from a write reply (for example `status`, `mutated`, `derived_sync`, `before_hash`) read them from `diagnostics.operational` after asking for `response_detail=full`.
- The `search_memory` and `read_memory` tools have no `response_detail` parameter, so `full` is not reachable on their refusals.
- Output schemas move slightly: `PageHit.scope` and `SemanticUnitHit.parent_ref` become optional, `FindEnvelope` gains `user_summary`, `notice` and `next_action`, and `ToolFailureEnvelope` becomes `{error, diagnostics?}`.
