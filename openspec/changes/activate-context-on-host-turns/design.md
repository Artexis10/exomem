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
  (vault identity hash, registry hash, index generation, anchor refs, roles). No
  secret: the server trusts nothing in it beyond matching its own generation, and it
  only qualifies anchors the current turn already reached. Persisted by the hook next
  to the continuation checkpoint, cleared on SessionStart, PreCompact and SessionEnd.
- **D4 — `anchor` override yields `agent_choice` evidence.** The agent is the decider;
  the override is refused for refs outside the index or withheld for the audience,
  without naming the page (same guard as referents).
- **D5 — Decision ledger reuses review-state.** Family `working_set`, refs
  `exomem://review/working-set/<fingerprint>`, closed reason vocabulary, manual origin,
  no due-state family, no emission. It is a ledger for the future verifier tier, not a
  feedback loop into activation.
- **D6 — Carrier line lives in bootstrap generic guidance.** One sentence at balanced
  and maximal; the compact ceiling has 512 bytes of headroom warning, so the line is
  budgeted and asserted by the existing compact-budget test.

## Risks / Trade-offs

- An old standalone hook copy on a client reads `working_set` as truthy stub mode:
  acceptable degradation, documented.
- Injecting 4,000 characters on every substantive prompt is the context-abstinence
  trade the benchmark exists to measure; the mode stays off by default until the
  compiler is accepted.
- The surface digest moves again for two optional arguments; landing in the same
  release as `add-context-activation` avoids a second connector refresh.

## Migration Plan

Additive. Hook mode off by default; no vault migration; token absence is the normal
first state. Depends on `add-context-activation` being merged; implementation
rebases onto it.

## Open Questions

- Whether the hook should honour `max_chars` from a preset per prominence level
  (maximal 4,000, balanced 2,000) rather than one default.
- Whether `agent_choice` should be recorded automatically in the `working_set` ledger.
