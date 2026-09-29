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

**Orchestrator rulings, 2026-09-29**, recorded here as binding on the build lane:

1. `focus` is current-turn evidence (D2.1). Every anchor carries an `origin` label (`turn`, `focus`, `turn_and_focus`, `conversation`), so the agent can tell what the user said from what the agent said was in play.
2. Assistant turns go into `recent`, capped. Only the newest two are read for evidence, and the carry never walks them (D1, D2).
3. The call ledger records `conversation` as name, length and sha256, the same as `turn`. There is no call-ledger contract change (D3).
4. The 1 s bound is the CI gate as pinned (D7). Live-cell latency is a separate lane, which this change must not regress.
5. S1 and S4 ship together, with one connector refresh (D7).
6. Fold `claude/keyless-thread-continuity` into this change, and specify S2's precedence on top of it. See D9 for what the branch turned out to contain.
7. The branch name stands, and authorship is fixed by squash merge.
8. **New:** clients may put attachment-derived cues into `focus` (D8).
9. **Instructions (ruling on #1455):** the 900-character bound on `SERVER_INSTRUCTIONS` stands; the owner is cutting injected bytes everywhere. The guidance lives in `activate_context`'s tool description. The instructions carry only a pointer, `In a long thread or with attachments, also pass \`conversation\` (see the tool).`, made room for by dropping the intro sentence and a redundant clause, with no rule lost. This supersedes the pinned sentence and the D5 fallback below.


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
   - **Origin labels (ruling 1).** Every served anchor carries `origin`:
     - `turn`: the user's words reached it;
     - `focus`: only the agent's `focus` reached it;
     - `turn_and_focus`: both reached it;
     - `conversation`: carried from an earlier user entry.

     Qualifiers never change an anchor's origin. A `focus` origin is never `agent_choice`, and `focus` cannot settle an ambiguity between anchors the turn segment itself resolved: that is what `anchor` is for. The hook render and the tool description say so, so the agent never mistakes its own cue for the user's statement.
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
   - **Precedence (ruling 6).** Activation decides a turn that its words left open with the first rule, in this order, that resolves or carries an anchor, or abstains `ambiguous`:
     1. the `anchor` override;
     2. resolution by the turn and `focus` segments;
     3. referential recency, including the continuity-thread resume;
     4. the shipped follow-up carry from the caller's own session tier;
     5. **the conversation carry**;
     6. the retrieval carry;
     7. abstention.

     Rules 3, 4 and 6 are the contracts on `main` (context-activation-continuity, with memory-loop in `close-memory-loop`), and they are unchanged. The conversation carry sits before the retrieval carry on purpose: the incident that abstained and then "activated unrelated context" is the retrieval carry serving an incidental hit when the user's subject was in an earlier turn. A conversation carry that abstains `ambiguous` stops the ladder.
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
- This change does not widen it. Ruling 3 keeps it: there is no call-ledger contract change.

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

**Superseded by ruling 9:** the pinned sentence below is not added to `SERVER_INSTRUCTIONS`; the tool description carries it and the instructions carry the short pointer.

One sentence is appended to `SERVER_INSTRUCTIONS`. The first sentence keeps "the user's message verbatim":

> In a longer conversation, or when the user's words lean on attachments, also pass `conversation`: `focus`, one line naming the subjects now in play, including names you read from attachments, and `refs`, the pages you already read; never rewrite `turn`.

The rest of the surface follows it:

- **Tool description.** It states:
  - the bounds, and that every field is optional;
  - that `focus` may carry names or objects the agent read from attachments;
  - that `focus`-origin anchors are the agent's cues, not the user's words;
  - that activation reads no attachment itself.

  The `turn` description keeps "verbatim".
- **Length.** The sentence is longer than the first draft. S4's red test proves the instructions still fit the existing length test. If they do not, the attachment clause moves to the tool description only, and the server sentence reverts to the shorter form.
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
- Two attachment cases: a nearly content-free turn ("thoughts on this?") with a fixture-authored `focus` of cues as a vision layer would read them, gold with `origin = "focus"`, and a twin whose cues name nothing in the corpus. They run on arms c and d. Arm (a) must abstain, as today's compiler correctly does.

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
| S1+S4 (one surface change, ruling 5) | Argument, bounds, `generation.conversation`, cache bypass, activation-log fields, withheld-equals-absent; `focus` segment with origin labels and attachment cues; `conversation` qualifier, promotion, tie-break; drowning share. Server instructions, tool description, scaffold line; regenerate schema fixture, tool-surface contract, plugin trees, hosted render, capabilities doc; ChatGPT two-phase rollout (`pending_tool_surface_sha256`, `refresh_required`) | yes, additive, one refresh |
| S2 | Anaphoric carry; the anaphor set; stop-at-first walk; the precedence ladder | no |
| S3 | Hook transcript tail for Claude Code, then Codex once its event is verified; origin labels in the render; mirror parity | no |
| S5 | Acceptance run of the group on arms a to d; latency gate pinned | no |

S1 and S4 ship as one surface change, so the owner refreshes the connectors once. The rollout needs the owner to refresh the ChatGPT Personal Plugin and the claude.ai connector. Until then, remote clients keep today's behaviour, because the argument is optional and unknown to them.

**Latency budget.** Measured on the model-free synthetic reference corpus with a maximum-size conversation:

| Stage | Budget (p95) | Why it fits |
|---|---|---|
| Bounding and parsing | < 1 ms | String slicing over ≤ 4 KB |
| Ref visibility filter | < 5 ms | ≤ 12 lookups in the in-memory catalogue view |
| Per-entry alias/lexical matching | < 40 ms | ≤ 5 entries × ≤ 600 chars through the existing `analyze_turn` n-gram path (≤ 4-grams over at most about 120 tokens each); no FTS, no embedding |
| Carry walk | < 15 ms | ≤ 3 user entries, stop at first; resolution over already-built candidate structures |
| **`working_set.conversation` total** | **≤ 60 ms** | Pinned in the CI latency gate |
| **Warm `activate_context` total** | **≤ 1,000 ms** | Pinned alongside; skipped-stage fallback if the reserve is short |

**Latency scope (ruling 4).**

- **What this change is accepted on.** The two CI-gate bounds above: warm `activate_context` p95 ≤ 1,000 ms and `working_set.conversation` p95 ≤ 60 ms, both on the synthetic reference corpus with a maximum-size conversation.
- **What it is not accepted on.** Live-cell end-to-end latency (still about 16 s p95 on the naive-path baseline) belongs to a separate lane, and this change makes no claim about it.
- **No regression.** The change must not regress live cells. A request without `conversation` does no conversation work and records no `working_set.conversation` span, which the spec pins. A request with one pays only the bounded stage above.
- **Existing ceilings.** The audit's existing ceilings (stage p50 800 ms and p95 2,500 ms, and the end-to-end naive baseline) are unchanged.

### D8. Attachment-derived cues travel in `focus` (ruling 8)

**The incident (anonymised).** The user sends screenshots with nearly content-free text. Activation sees only the text and correctly abstains, while the client's vision layer could read names and objects from the images.

**The fix.** It adds no new channel. The client may put what its vision or file layer read into `focus`:

- The cues resolve as `focus` evidence with the same worded kinds and are labelled `origin = "focus"`.
- They are never the user's words, never an `anchor` choice, and never authority for anything beyond contact.
- They are request-scoped like all conversation content.
- Activation receives, fetches and decodes no attachment, and runs no OCR, CLIP, captioning or other media model, so the request path gains no media stage and no model.

**Rejected alternatives.**

- A separate `attachments` field. That is another surface to refresh, for text the server would treat exactly like `focus`.
- Server-side OCR or CLIP on attachments during activation. That puts a model on the request path, breaks p95, and sends user media through the server's media transducers outside their governed ingestion path.

### D9. The keyless-thread branch (ruling 6), audited against `main`

`claude/keyless-thread-continuity` (`72fd41ec`, three commits, based on `1abb81c7`) was audited on 2026-09-29 before folding it in:

- **Its behaviour has already landed on `main`, in a later and hardened form**, through #1424 ("keyless continuity, incident routing…", `84f95bc9`):
  - Every test function in the branch's `tests/test_working_set_keyless_continuity.py` exists on `main`.
  - `main` adds four more: `test_a_rewritten_thread_is_served_as_keyless`, `test_a_signed_thread_lives_only_within_its_bounds`, `test_an_unsigned_thread_is_served_as_keyless` and `test_the_token_crosses_the_dispatcher_and_continues_the_thread`.
  - The branch's `working_set_resolve.py` equals `main`'s.
  - Its keyed/keyless cache separation (the third commit) is on `main` as the `:stranger` heat-digest salt in `working_set_runtime.py`.
  - The follow-up carry and the vault-tier rule are in `close-memory-loop`'s memory-loop delta on `main`.
- **A trial merge into current `main` conflicts in six files:**
  - `close-memory-loop/specs/context-activation-continuity/spec.md`
  - `commands.py`
  - `working_set_runtime.py`
  - `tool_surface_contract.json`
  - `deploy/chatgpt/personal-plugin-contract.json`
  - the keyless test, as an add/add conflict

  On the spec, the branch's side is the *earlier* wording. It lacks the authenticated-thread clause, the `withheld` exception and the rewritten-thread scenario.
- **Consequence.** Merging the branch as-is would at best be a no-op after conflict resolution toward `main`, and at worst would reintroduce the unauthenticated thread contract.

This change therefore specifies S2's precedence on top of the landed contract, which is what the branch intended. Task 3.0 makes the build lane re-verify the audit before S2 and merge only a residual, if one appears. Whether the branch should instead be retired unmerged is raised to the orchestrator.

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
- **The call-ledger hash of the argument.** It stays within existing policy (ruling 3). A short `focus` is as guessable from its hash as a short `turn` already is.
- **Attachment cues are only as good as the client's vision layer.** A misread name reaches nothing (a twin pins it) or reaches a wrong anchor labelled `focus`. The label is what lets the agent discount it.

## Migration

The change is additive. Old clients and old services interoperate: the argument is optional, and the hook retries once without the field on an old service. Remote connectors pick up the field only after the owner-side schema refresh in S4.


## Post-hoc corrections (orchestrator rulings on #1455)

**Fixture correction, `context-activation-conversation-v1`.** The first acceptance run failed twins W17 to W24 on arms (c) and (d). Each twin's turn is content-free, but it carried the paired case's fixture `focus` naming the earlier subject. `focus` is current-turn evidence (ruling 1), so an exact alias in it resolves that anchor, and the twin resolved its own poison. The named focus was a confound: a twin exists to test a content-free turn. The twins' `focus` was therefore re-authored to `closing pleasantries`, which names nothing. One case, V29 (attachment group, arms a, c, d), was added: the same content-free turn with a focus that names the earlier subject, pre-registered so that arms (c) and (d) resolve it with `origin = "focus"` and arm (a) abstains. That keeps ruling 1 pinned. It is a case in the attachment group rather than a twin because the twin invariants (one twin per case, a twin never serves its case's gold) forbid a twin that resolves its case's gold. The fixtures were edited after the first scored run, so that run is void: the digest moved from `374056f5…` to `1c81d3e9…`, the arm (a) baseline and the acceptance run were re-recorded, and the eight rows left `PENDING_RULING`.

**Anaphor set.** The bare pointers "one", "ones" and "other" no longer make a turn anaphoric by themselves (the shipped follow-up test, `is_follow_up`, is unchanged). They count only when a determiner, demonstrative, ordinal or "which" governs them. This removes the false positive on a numeral use ("plenty of chat for one day"), which the first acceptance run found on W24 (arm b).
