## Context

`activate_context` (`commands.py`, `working_set*.py`) compiles a working-memory packet from three inputs, none of which carries the conversation's content:

- the current turn, verbatim and uncapped (only downstream stages are bounded: a 40-token embedding window, at most 12 lexical terms and 40 stems, and 8 recall hits);
- a client-carried `continuity` token (the refs of resolved anchors, plus a keyless thread identity that feeds the heat projection);
- an optional `anchor` the agent chose.

The recency rules in `close-memory-loop` let a bare referential turn ("continue") or a short follow-up (at most 8 tokens, at most one content word) be carried from the caller's own session tier. A rich, anaphoric turn falls through all of those rules: "Given everything above, how did her results compare…" has too many words to be a follow-up and too few names to resolve. Its subject lives in earlier turns, which the server never sees.

Three facts shape the design:

- Hooked clients (Claude Code, Codex) have a local transcript. The continuation checkpoint hook already opens its final 64 KiB safely.
- Remote clients have only the tool call, which the agent composes. `SERVER_INSTRUCTIONS` (`server.py:503`) currently asks for the message verbatim.
- The constitution (`openspec/config.yaml`, restated by `add-sensed-epistemic-model`) forbids server-side text-to-text generation. Models are instruments that answer closed questions off the request path. The compiler must stay categorical and deterministic.

## Goals / Non-Goals

**Goals**

- The agent can hand the compiler a small, bounded view of the conversation, whether it is hooked or remote.
- The compiler uses that view for three things only:
  - to resolve a referent the current turn leaves open;
  - to complete a multi-domain set the current turn half-reached;
  - to break a tie the current turn left.
- The current turn always outranks the conversation.
- Nothing about the conversation outlives the request.
- Warm activation stays under 1 s at p95.
- The change is measured on a pre-registered group before and after.

**Non-Goals**

- **Server-side coreference or ordinal resolution over assistant-authored lists.** "The second option" is resolved by the agent (through `focus` or `anchor`), not by the server.
- **A server-side conversation store.** The server stays stateless per conversation.
- **Instrument (model) use at request time.**
- **Changing `ask_memory`/`find`, the continuity token format, or the keyless-thread semantics.**
- **Capturing conversation content as memory.** That remains `episode_memory`'s job, authored by the agent.

## Decisions

### D1. The signal: one optional `conversation` object with three bounded fields

| Field | Who fills it | Bound | Truncation |
|---|---|---|---|
| `focus` | Remote agent (the reasoner): one line naming the subjects now in play | 240 chars | Cut at last whitespace ≤ bound |
| `recent` | Hooks, from the transcript; remote agents MAY, but are not asked to | ≤ 6 entries `{role, text}`; user ≤ 600 chars, assistant ≤ 300; ≤ 2,400 total | Per-entry head kept at a whitespace cut; entries dropped oldest-first until count and total fit |
| `refs` | Hooks (from Exomem read-call arguments); remote agents (pages they read) | ≤ 12 refs | Dedupe keep-first, drop from end |

- Every field is optional, and the whole object is optional. The server enforces every bound itself and reports `generation.conversation ∈ {applied, truncated, absent}`.
- A malformed object is `absent`, never an error, so a buggy client degrades to today's behaviour.
- In the worst case, 240 + 2,400 + 12 refs of about 120 chars is roughly 4.1 KB, or about 1,100 tokens on the wire.

**Why three fields, not one.** Each field is the cheapest faithful signal for one client class:

- **`focus`** is the only thing a remote agent can produce without pasting the conversation back. It costs about 60 tokens in the tool call, and the agent is already the sanctioned reasoner.
- **`recent`** is what a hook can read without any model. It carries the raw evidence, so the compiler, not the hook, decides what matters.
- **`refs`** is the most precise signal of what the conversation has actually touched. It is a closed identifier, not prose, so it is also the safest to carry.

**Why assistant text is capped harder.** Assistant replies are long and model-authored. A 300-character head keeps the subject line of a reply ("For the Kestrel hiring plan, the three options are…") without letting an assistant digression dominate. Only the newest two assistant entries are read for evidence (D2).

**Why the name `conversation`.** `thread` already names the keyless continuity thread (`generation.continuity_thread`, `working_set_runtime.py`). A second meaning would make every packet ambiguous to read.

### D2. How the compiler uses it, with the current turn first

The compiler adds one stage, `working_set.conversation`, between `working_set.resolve` evidence assembly and status derivation. Resolution then follows the rules below.

1. **`focus` is part of the current turn.** It is resolved as a second segment with worded kinds only: `exact_alias`, `lexical_overlap` and `claims_match`. It gets no second recall query and no second embedding. Segments never pair: no n-gram, proximity window or rarity pair crosses them. The referential, follow-up and retrieval-carry tests read the turn segment alone. `focus` is treated as current rather than historical because the agent writes it for this turn. It is the remote client's only lever, and the agent may already set `anchor`, which is stronger.
2. **`recent` and `refs` yield one qualifier, `conversation`.**
   - Sources:
     - a visible ref equal to an anchor's canonical ref;
     - an `exact_alias` or `lexical_overlap` hit inside one entry (per entry, never across entries);
     - only the newest three user and the newest two assistant entries.
   - As a qualifier it never creates a candidate and never resolves alone.
   - It shares the `continuity` exception: one current-turn contact plus `conversation` resolves. Together with `continuity` it counts once.
3. **Promotion (multi-domain completion).** A second domain the turn reached only `partial` becomes `resolved` when the conversation named it. This is the "half a decision" incident.
4. **Tie-break.** In an `ambiguous` turn, if exactly one competitor carries `conversation`, that competitor is served and the rest are listed `partial`, with `generation.disambiguated_by = "conversation"`. If none or several carry it, the turn stays ambiguous.
5. **Carry (same-thread promotion).**
   - Trigger: an anaphoric turn of any length that reaches nothing by its own words (a pronoun or possessive, a demonstrative, a shipped follow-up marker, an ordinal plus "one/option", or "the former/latter").
   - Precondition: no earlier rule (recency, continuity, follow-up) has already carried it.
   - The compiler walks the user entries newest to oldest and stops at the first entry that resolves anchors on its own text. That entry's single anchor is carried `partial` with evidence `[conversation]` and `carried_by = "conversation"`. Two or more make the turn `ambiguous`, and the compiler does not choose. Nothing resolving means today's abstention.
   - Assistant entries are not walked: the subject must have been the user's.
   - Refs never carry: a read is not a referent.

**Drowning guard.**

- Only the newest few entries are read at all, and each entry is matched on its own.
- The conversation never adds an anchor the turn did not reach, except by the carry, which runs only when the turn reached nothing.
- When the turn resolved something itself, conversation-promoted material is capped at one third of `max_chars`, served after the turn's own anchors.
- A long conversation therefore cannot raise an old subject's weight. Length only changes which entries are the newest.

**Why no float weighting (recency decay, similarity).** The resolver's contract is categorical evidence with no float in the packet (`context-activation`: "Categorical anchor evidence and resolution"). A decayed score would reintroduce the float and would need tuning per client. The newest-entries window and the stop-at-first carry express recency categorically.

### D3. Privacy and retention: ephemeral by default

**Nothing new is written.**

- Conversation content lives only in the request.
- It never enters the activation index, the packet cache, the heat projection (refs are not picks or reads), the activation log (only presence, counts and `generation.conversation`), the episode ledger, capture state, or timings.

**What the call ledger keeps.**

- The ledger already records every argument as name, byte length and sha256 (call-ledger). That is existing policy, and it covers `conversation` exactly as it covers `turn`.
- This change does not widen it. Tightening it to name and length only for `conversation` is offered as a ruling (see "Needs ruling" in the PR), because it would be a call-ledger contract change.

**The token.** The continuity token never encodes a conversation field, a carried anchor, or an anchor that resolved only with the conversation's help. A conversation that dropped its signal then degrades to today's behaviour instead of laundering that signal into durable client state.

**The packet cache is bypassed.**

- A packet built with a conversation is compiled afresh and never stored.
- A cache key that included a conversation digest would persist a digest of the text in memory, and it would almost never hit, because every turn's conversation differs.

**Withheld equals absent.**

- A ref to a withheld page is dropped before evidence, just like an unknown ref.
- Entry text is matched against the caller's view of the catalogue (the existing "caller's view" rule).
- `generation.conversation` is computed before visibility filtering, so it cannot reveal a drop.
- The packet is byte-identical to the one for a conversation that never named the page.

**Episode ledger interaction.** None by design. `episode_memory` stays agent-authored, and hooks never forward conversation text into capture. The keyless thread and the heat projection are unchanged: the conversation neither records into nor reads from them.

### D4. Hooks supply it automatically from the transcript

`exomem_retrieve_nudge.py`, in `working_set` mode, and only where the prompt event carries `transcript_path`:

- **Reading.** It reads the transcript's final 64 KiB through the checkpoint hook's safe regular-file open. It parses by line, discarding the first partial line, and applies a closed parser per client format.
- **Selection.**
  - It keeps human-typed user text and the assistant's final text only.
  - It drops tool calls and results, thinking blocks, system and hook messages, and binary payloads.
  - It drops every block carrying Exomem's fixed data header. Without that exclusion the hook would feed its own previous injection back as "conversation", a self-reinforcing loop.
  - It drops the current prompt if the client already appended it.
- **Refs.** Refs come only from Exomem read-call arguments (`read_memory` paths, `anchor` values) in the tail, never parsed out of result text.
- **No focus.** The hook runs no model, so it never sends `focus`.
- **Budget.** 50 ms of wall time inside the existing injection budget (REST 4.0 s, inject budget 8.0 s). On any failure it sends no conversation, never a partial parse.
- **Retry.** An older service that rejects the field gets one retry without it, as for the attribution fields.
- **Persistence.** None. The token file stays the only hook state for activation.

The plugin mirror stays byte-identical, pinned by `tests/test_plugin_sync.py`.

**Codex.** The same script serves Codex. The checkpoint hook's event adapter already accepts an optional `transcript_path` for Codex events. Whether Codex's `UserPromptSubmit` delivers it, and in what rollout format, is verified in S3 against a recorded fixture. Where it is absent, Codex hooks send no conversation, and the gap is reported rather than papered over.

### D5. Server-instruction wording for remote agents

One sentence is appended to `SERVER_INSTRUCTIONS`. The first sentence keeps "the user's message verbatim":

> In a longer conversation also pass `conversation`: `focus`, one line naming the subjects now in play, and `refs`, the pages you already read; never rewrite `turn`.

The rest of the surface follows it:

- **Tool description.** It states the bounds and that every field is optional. The `turn` description keeps "verbatim".
- **Scaffold skill line** (`_scaffold/_Schema/SKILL.md`). It is updated to match, generic, with no leak tokens.
- **Benchmark arm.** A3's arm prompt stays verbatim-only in the existing groups. The new group adds an arm that passes `focus` as specified here (D6).
- **What remote agents are not asked for.** They are not asked for `recent`: pasting earlier turns costs the most tokens and duplicates what `focus` says better. `recent` stays open to them, bounded, for clients that want it.

**Why `focus` and not "summarise the conversation into `turn`".** Rewriting `turn` destroys the verbatim signal that exact-alias resolution, twins and the referential test depend on. That is the reason the instructions say "verbatim" today.

### D6. Pre-registered benchmark group `conversation`

The group lives in `benchmarks/epistemic/corpora/context_activation.py` (or a sibling module mirroring the multilingual one). `FixtureCase` gains an optional `conversation` field, and cases have their own ids (`V1…`, twins `W1…`).

**Contents.**

- Twelve rich single turns of two to five sentences, at least four spanning two domains.
- Twelve multi-turn conversations of three to six entries.
- Each case has a negative twin (an unrelated conversation, or a current turn that reaches nothing).
- At least three drowning cases and three topic-switch cases.
- One withheld-versus-absent pair, scored for byte identity.

**Pre-registration.**

- Each case carries gold, poison, must-include, must-exclude, expected status and expected `carried_by`.
- All names are invented: "Harbor Lantern budget", "Kestrel hiring plan", "Ottilie Marsh" and "Tidewater grant" are the style.
- The group has its own `FIXTURE_SET_ID` and digest.
- The digest is pinned in a test in the pre-registration commit, before the first scored run. A later edit voids runs instead of rescoring them, which is the audit's existing rule.

**Arms.**

- (a) Turn only: today's behaviour, and the mechanism-removal control.
- (b) Turn + `recent` + `refs`, as a hook sends them.
- (c) Turn + `focus` authored in the fixture, as a remote agent would.
- (d) Turn + all three.

**Floors (per case and per anchor kind, no aggregate).**

- Gold recall ≥ 0.85 on arms b to d.
- Zero poison and zero twin false resolution.
- Zero drowning failures.
- Arm (a) failing at least half of the multi-turn cases.
- The existing packet-size bounds.

A baseline run of arm (a) on the current compiler is recorded in S0, so the incident classes are shown to fail before any product code lands.

### D7. Slices and the latency budget

| Slice | Content | Tool surface? |
|---|---|---|
| S0 | Group fixtures, digests, scorer arms; baseline of arm (a) on `main` | no |
| S1 | Argument, bounds, `generation.conversation`, cache bypass, activation-log fields, withheld-equals-absent; `focus` segment; `conversation` qualifier, promotion, tie-break; drowning share | yes, additive |
| S2 | Anaphoric carry; the anaphor set; stop-at-first walk | no |
| S3 | Hook transcript tail for Claude Code, then Codex once its event is verified; mirror parity | no |
| S4 | Server instructions, tool description, scaffold line; regenerate schema fixture, tool-surface contract, plugin trees, hosted render, capabilities doc; ChatGPT two-phase rollout (`pending_tool_surface_sha256`, `refresh_required`) | yes |
| S5 | Acceptance run of the group on arms a to d; latency gate pinned | no |

S1 and S4 can ship as one surface change if the owner prefers a single connector refresh. The rollout needs the owner to refresh the ChatGPT Personal Plugin and the claude.ai connector. Until then, remote clients keep today's behaviour, because the argument is optional and unknown to them.

**Latency budget.** Measured on the model-free synthetic reference corpus with a maximum-size conversation:

| Stage | Budget (p95) | Why it fits |
|---|---|---|
| Bounding and parsing | < 1 ms | String slicing over ≤ 4 KB |
| Ref visibility filter | < 5 ms | ≤ 12 lookups in the in-memory catalogue view |
| Per-entry alias/lexical matching | < 40 ms | ≤ 5 entries × ≤ 600 chars through the existing `analyze_turn` n-gram path (≤ 4-grams over at most about 120 tokens each); no FTS, no embedding |
| Carry walk | < 15 ms | ≤ 3 user entries, stop at first; resolution over already-built candidate structures |
| **`working_set.conversation` total** | **≤ 60 ms** | Pinned in the CI latency gate |
| **Warm `activate_context` total** | **≤ 1,000 ms** | Pinned alongside; skipped-stage fallback if the reserve is short |

The existing audit ceilings (stage p50 800 ms and p95 2,500 ms, and the end-to-end naive baseline) stay as they are. The new 1 s bound is warm, synthetic and gate-local. Its relation to the brief's "activation p95 under 1 s" on live cells is raised for a ruling.

## Rejected alternatives

- **Send the whole transcript.**
  - Tokens and latency are unbounded, and privacy exposure is maximal.
  - Remote agents would have to paste the conversation back into every tool call.
  - Rejected for the hard constraints on bounded tokens and p95.
- **Concatenate earlier turns into `turn`.**
  - It destroys the current turn's primacy: exact-alias n-grams span turns, and twins stop being twins.
  - The referential and follow-up tests stop working, because the turn is no longer short or cue-only.
  - It also invites drowning.
- **Server-side summarisation of the conversation.** A generative model on the request path is banned (no server-side text-to-text generation) and would break p95.
- **A closed-question instrument at request time** (for example `mention.same_referent` from `add-sensed-epistemic-model`).
  - Instruments run off the request path in a disposable worker under hourly budgets, and their readings are about vault units, not live conversation text.
  - Running one per turn would break p95, and would feed unpersisted user text to a model outside the instrument contract.
- **A server-side conversation store keyed by session.**
  - It violates "the server keeps no per-conversation state" (context-activation-continuity) and "ephemeral by default".
  - Remote callers are often keyless anyway.
- **Rely on the continuity token alone.**
  - The token carries only anchors a packet resolved. A subject established in a turn that abstained, or in assistant text, never enters it.
  - Remote clients often drop it.
  - It is the mechanism that failed in the incidents.
- **Embed the conversation (a vector band over earlier turns, or a recency-weighted centroid).** It costs one more embedding per turn and introduces a float score. A centroid of a long conversation is exactly the drowning failure mode.
- **Make the conversation a contact kind (resolve on conversation plus one qualifier).** A long conversation would then add anchors the current turn never reached, which is drowning by construction.
- **Hooks synthesise `focus` with a local model.** No model in hooks. `focus` is reserved for the agent, which already reasons over the conversation.
- **Ask remote agents for `recent` as well as `focus`.** It costs more tokens on every call for evidence that `focus` states more precisely, and it is harder to keep verbatim. `recent` remains accepted, but the instructions do not ask for it.
- **Include a conversation digest in the packet cache key.** It keeps a digest of user text in process memory and almost never hits. Bypassing the cache is simpler and leaves nothing behind.
- **Name the argument `thread`.** It collides with the continuity token's keyless thread.

## Risks / Trade-offs

- **`focus` is trusted as current-turn evidence.** A careless agent could steer activation with it. This is bounded: `focus` gets worded kinds only, 240 characters, and no recall or embedding. The agent can already do more with `anchor`. The benchmark's twin arms with a misleading `focus` measure it.
- **Anaphor false positives.** "It" in "is it worth it?" makes the turn anaphoric. The carry still requires the turn to reach nothing and an earlier user entry to resolve on its own, and it serves `partial` only. Drowning and twin floors pin the rate.
- **Transcript formats drift.** Closed parsers soft-fail to "no conversation", so a format change costs the feature and never correctness. Recorded fixtures per client pin the parser.
- **Codex may not deliver `transcript_path` on `UserPromptSubmit`.** Then Codex gets no automatic conversation until it does, which is reported in S3.
- **The call-ledger hash of the argument.** It stays within existing policy. The ruling covers tightening it.

## Migration

The change is additive. Old clients and old services interoperate: the argument is optional, and the hook retries once without the field on an old service. Remote connectors pick up the field only after the owner-side schema refresh in S4.
