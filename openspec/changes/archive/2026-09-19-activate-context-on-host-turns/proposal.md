# Proposal: activate-context-on-host-turns

## Why

`add-context-activation` gives Exomem a read-only `activate_context` tool, but a tool
the agent must remember to call is still a nudge. Where the host exposes a prompt
lifecycle (Claude Code and Codex hooks) the compiled packet can be injected before
inference deterministically; where it does not (claude.ai and ChatGPT connectors) the
best available contract is an honest carrier line in the served guidance. Two things
the first slice deferred also belong here because they only matter once turns arrive
in sequence: a client-carried continuity token so consecutive turns do not re-resolve
the world, and an `anchor` override so the active agent, the only decider, can
resolve an `ambiguous` turn without the server guessing. Recording which anchors the
agent accepts or rejects turns the compiler's decisions into the training signal the
later verifier tier needs.

## What Changes

- The Claude Code and Codex `UserPromptSubmit` retrieve hook gains a **working-set
  injection mode** (`EXOMEM_RETRIEVE_INJECT=working_set`, default off): it calls
  `/api/activate_context` with the prompt text over the existing REST-first transport
  and budget, injects the packet under a fixed data header bounded by `max_chars`
  (default 4,000), falls back to the existing reminder on any failure, and stays
  silent under the same prominence, prompt-length, cooldown and control-prompt gates
  as today. The hookless surfaces get one carrier line in bootstrap's generic client
  guidance at balanced and maximal prominence.
- `activate_context` gains two optional arguments: `continuity` (an opaque token the
  previous packet returned) and `anchor` (a canonical ref the agent chooses for an
  ambiguous turn). The packet returns a `continuity` token. Continuity contributes only
  the `continuity` evidence kind, a qualifier that never resolves an anchor on its
  own; a token from another vault, another registry hash or an older index generation
  is ignored and reported. The hook persists the token beside the continuation
  checkpoint keyed by client and session, and drops it on compaction or a new session.
- The decision ledger first sketched here (a `working_set` review family) is deferred
  to `add-consolidation-dreamer`, where its consumer lives: review families are signal
  families tied to due-state, and a read-only operation must not write.
- Non-goals: hosted-profile publication of the tool (a new profile decision), the hot
  profile, any server-side model, any change to `ask_memory`.

## Capabilities

### New Capabilities
- `context-activation-continuity`: the continuity token and the `anchor` override.

### Modified Capabilities
- `retrieve-inject-hook`: adds the working-set injection mode beside the existing
  stub mode, with the same defaults, transport order, budget and silence rules.
- `agent-bootstrap-contract`: generic client workflow guidance carries the
  activation line at balanced and maximal prominence.

## Impact

- `plugins/claude-code/hooks/exomem_retrieve_nudge.py` and its byte-identical copy
  under `src/exomem/_hooks/`, `install_hook.py` (Codex wiring unchanged, mode env
  documented), `exomem_continuation_checkpoint.py` (token persistence).
- `src/exomem/commands.py` (`op_activate_context` arguments), `working_set_resolve.py`
  (`continuity` evidence, `anchor` override), `working_set_runtime.py` (token
  encode/decode, cache keying), bootstrap guidance text.
- Tool-surface digests move again (two new optional arguments); schema fixture,
  capabilities and the pending connector digest regenerate; ideally lands in the same
  release as `add-context-activation` so the connector refreshes once.
- `add-context-activation` is merged (PR #1282); this change builds on main.
