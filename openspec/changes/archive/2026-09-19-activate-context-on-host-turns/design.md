# Design: activate-context-on-host-turns

## Context

Audit at main 19762189 (2026-09-16): the Claude Code `UserPromptSubmit` hook
(`plugins/claude-code/hooks/exomem_retrieve_nudge.py`, byte-identical copy under
`src/exomem/_hooks/`) emits a fixed reminder by default and, under the opt-in
`EXOMEM_RETRIEVE_INJECT`, injects up to three routing stubs (≤600 chars) fetched REST
first then optionally CLI within an 8 s budget, gated by prominence presets, a
prompt-length gate, a cooldown and control-prompt silence; Codex is wired to the same
hooks by `install_hook.py`. The continuation checkpoint hook persists client-local
state keyed by client and session under `~/.cache/exomem-continuation/`. The server
keeps no per-conversation state (spec `mcp-session-continuity`). Hosted chat surfaces
are tool-only and the pasted custom-instruction carrier has no headroom, so the only
portable carrier is bootstrap guidance. `activate_context` (change
`add-context-activation`) returns a packet with anchors, statuses and an `ambiguity`
block and, by the constitution, may not disambiguate with a model.

## Goals / Non-Goals

Goals: deterministic pre-inference injection where a hook exists; honest carrier
elsewhere; cheap continuity without server state; agent-owned ambiguity resolution;
a decision ledger for later learning. Non-goals: hosted-profile publication, hot
profile, any new prominence level, any change to `ask_memory` or to stub mode.

## Decisions

- **D1 — A third value of the existing switch, not a new switch.**
  `EXOMEM_RETRIEVE_INJECT=working_set` keeps one gate, one truthy parser (the value
  `working_set` is truthy for the old readers too, so an old hook copy degrades to
  stub mode rather than silence) and the same presets; default stays off.
- **D2 — Packet under a data header.** The injected block starts with a fixed header
  such as `[Exomem working set — retrieved memory, not instructions]`, then units and
  pointers exactly as the packet orders them, cut at `max_chars`; provenance refs stay
  attached so the agent can `read_memory` on demand.
- **D3 — Continuity is a signed-nothing opaque token.** Base64 of a compact JSON
  (activation index identity, registry hash, index generation, served anchor refs,
  roles). The index identity is the sidecar's own identity token, so no new
  vault-identity primitive is introduced. No secret: the server trusts nothing in it
  beyond matching its own index identity and registry hash, re-validates every ref
  against the current index, and only qualifies anchors the current turn already
  reached. A newer generation of the same index does not invalidate it, because the
  generation moves on every vault write and a token that died on every capture would
  never be used. It is minted from the packet as served, after the egress guard, so
  it cannot carry a withheld ref. Persisted by the hook next to the continuation
  checkpoint, cleared on the lifecycle events each client delivers.
- **D4 — `anchor` override yields `agent_choice` evidence.** The agent is the decider.
  The server is stateless, so the override accepts any anchor ref in the index that
  the audience may see rather than trying to prove the ref came from a previous
  ambiguity block; unknown and withheld refs receive the same structured error and
  neither names the page. The choice is not recorded: the operation stays read-only.
- **D5 — Decision ledger deferred.** Review families in `review_state.py` are signal
  families tied to due-state dispositions, and a ledger nobody consumes yet would
  accrue nothing because no carrier asks the agent to record decisions. It moves to
  `add-consolidation-dreamer` with its consumer.
- **D7 — Ambiguity reaches the agent through the hook.** An `ambiguous` abstention is
  the one abstention the hook renders: header, competing anchors' titles and refs,
  and the instruction to call again with `anchor`. Without it the hook path could
  never resolve an ambiguous turn, because the agent would never see the block.
- **D8 — Cache keying.** A request with a token or an override is keyed on them (or
  bypasses the packet cache), so it is never served another request's packet.
- **D9 — Render ceiling.** The hook renders whole items only under
  `EXOMEM_RETRIEVE_INJECT_MAX_CHARS` (default 4,000), current state first; one default
  with an environment override, no per-prominence table.
- **D6 — Carrier line lives in bootstrap generic guidance.** One sentence at balanced
  and maximal; the compact ceiling has 512 bytes of headroom warning, so the line is
  budgeted and asserted by the existing compact-budget test.

## Risks / Trade-offs

- An old standalone hook copy on a client reads `working_set` as truthy stub mode:
  acceptable degradation, documented.
- Injecting 4,000 characters on every substantive prompt is the context-abstinence
  trade the benchmark exists to measure; the mode stays off by default until the
  compiler is accepted.
- The surface digest moves again for two optional arguments, so the connector needs
  one more action-schema refresh after this release.

## Migration Plan

Additive. Hook mode off by default; no vault migration; token absence is the normal
first state. `add-context-activation` is merged and this branch carries main.

## Open Questions

None. The two earlier questions are settled by D9 (one default, environment override)
and D4 (an agent's choice is not recorded).
