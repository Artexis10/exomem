## Why

The context compiler (`activate_context`) runs before every substantive turn. Today it sees three things: the user's current message verbatim, an opaque `continuity` token carrying the refs of anchors that earlier packets resolved, and an optional `anchor` the agent chose. It never sees the conversation thread.

That fits a terse coding session in Claude Code or Codex, where "continue" is common and the token carries the subject. It does not fit how the owner mostly works in ChatGPT and the Claude apps: rich, multi-sentence turns inside long threads. Three incident classes follow from the gap:

- **Lost thread subject.** A follow-up about a topic established earlier in the same thread abstained `unresolved`, then activated unrelated context. The subject lived in an earlier turn (or in the assistant's reply), not in the current one or in any resolved anchor.
- **Half a decision.** A turn weighing two domains surfaced one domain and missed the other, because the second domain reached only `partial` on the current turn's words.
- **Thread-bound referents.** "That", "the second option" and "her results" resolve only against the thread. The current-turn compiler has nothing to resolve them against, and the keyless follow-up carry can only replay what an earlier packet already resolved.

Hooked clients (Claude Code, Codex) can read a local transcript. Remote clients (ChatGPT, the claude.ai connector) can pass only what the agent puts into the tool call, and the server instructions currently tell the agent to pass the message verbatim and nothing else. The context-activation benchmark consists of short, cold single turns plus a small continuity group, so it has never measured this failure class.

## What Changes

- **One new, optional, bounded `conversation` argument on `activate_context`** (additive; omitting it is byte-identical to today). It has three fields, each capped:
  - `focus`: the agent's own one-line statement of what the conversation is about now (at most 240 characters);
  - `recent`: the tail of earlier turns, newest last (at most 6 entries: user turns up to 600 characters each, assistant turns up to 300; at most 2,400 characters in total);
  - `refs`: canonical page refs the conversation already read or cited (at most 12).

  The server enforces every bound itself, truncating deterministically. It reports `generation.conversation` as `applied`, `truncated` or `absent`, and never refuses a request over it. The name is `conversation`, not `thread`, because "thread" already names the continuity token's keyless thread (`generation.continuity_thread`).
- **Conversation evidence is subordinate to the current turn** in the compiler, under closed rules:
  - `focus` is the agent's statement of the current turn, so it is resolved as a second segment of the current turn. Anchors it reaches carry the source tag `focus`.
  - `recent` and `refs` contribute only a new qualifier evidence kind, `conversation`, which never creates a candidate and never resolves on its own.
  - The `conversation` qualifier has exactly three effects:
    1. **Promotion.** An anchor the current turn reached by one contact kind resolves with `conversation`, as it does with `continuity` today. This recovers the missed second domain.
    2. **Tie-break.** An `ambiguous` turn whose competitors include exactly one conversation-bearing anchor resolves to that anchor.
    3. **Carry.** An anaphoric turn (any length) that reaches nothing by its own words is carried from the newest earlier user turn that resolves an anchor. The carried anchor is a single `partial` anchor with `generation.carried_by = "conversation"`.
  - Conversation-promoted material is capped at a third of the packet budget whenever the current turn resolved anything itself.
  - Earlier turns are matched by alias and lexical kinds only, per entry, newest three user and two assistant entries only: no embedding, no recall query, no model. `focus` gets worded kinds only (no second recall or embedding).
- **Ephemeral by default.** Conversation content is request-scoped:
  - It is never written to the activation log, heat projection, episode ledger, packet cache or any state file. The activation log records only presence, counts and `generation.conversation`. The call ledger keeps its existing shape-and-hash record of every argument, which covers `conversation` exactly as it already covers `turn`.
  - A request carrying a conversation is neither served from nor stored in the packet cache.
  - A conversation ref to a withheld page is dropped before evidence exactly as an unknown ref is: no marker, and no difference in any `generation` field.
  - The continuity token never encodes a carried or conversation-only anchor.
- **Hooks supply the conversation automatically.**
  - The Claude Code and Codex retrieve hooks, in `working_set` mode, read a bounded tail of the local transcript. They take the last user and assistant text turns (excluding tool payloads and every Exomem-injected block) and the refs of Exomem reads in the same tail, then send them as `conversation.recent` and `conversation.refs`.
  - Hooks never synthesise `focus`: they run no model.
  - Parsing is time-boxed. On failure the hook sends no conversation, never a partial parse.
- **Server instructions and tool description** ask remote agents to pass the user's message verbatim as `turn` and, in a long conversation, a one-line `focus` naming the subjects in play, plus the refs they already read. The wording is pinned in this change.
- **A pre-registered benchmark group, `conversation`**, of rich multi-sentence single turns and multi-turn threads:
  - every case has gold, poison and a negative twin, and uses invented names;
  - it includes drowning cases (a long conversation about one subject, then a current turn about another), topic-switch poison and a withheld-versus-absent twin;
  - fixture digests are pinned in a commit that precedes the first scored run;
  - a mechanism-removal arm strips the conversation and must turn the group red.
- **A measured latency budget.** The conversation stage adds at most 60 ms at p95 on the warm synthetic reference corpus, and warm activation with a full-size conversation stays under 1 s at p95. The CI latency gate pins both.
- **Delivery in slices:**
  - S0: pre-registration and a baseline on the current compiler.
  - S1: the argument, bounds, privacy and the promotion and tie-break rules.
  - S2: the conversation carry and focus.
  - S3: hooks.
  - S4: instructions and the connector rollout.
  - S5: the acceptance run.

## Capabilities

### New Capabilities

- `conversation-aware-activation`: the `conversation` argument and its bounds, how conversation evidence enters resolution, retention and egress of conversation content, remote-agent guidance, the pre-registered benchmark group and the latency budget.

### Modified Capabilities

- `retrieve-inject-hook`: the `working_set` injection mode supplies a bounded conversation tail read from the local transcript (Claude Code and Codex), time-boxed and fail-silent. The requirement is added beside the `close-memory-loop` amendment of the mode, not in place of it.

`context-activation` and `context-activation-continuity` are not modified here. The `close-memory-loop` change already holds `MODIFIED` blocks on both, and a second competing block would conflict at archive. The conversation contract is stated in the new capability instead and references them.

## Impact

- **Code (later slices, not this round):**
  - `working_set_resolve.py`: the `conversation` qualifier and the focus segment;
  - `working_set.py` and `working_set_runtime.py`: argument parsing, bounds, cache bypass, the carry and the budget share;
  - `commands.py`: the leaf and CLI flags;
  - `server.py`: `SERVER_INSTRUCTIONS` and the tool description; the scaffold `SKILL.md` activation line;
  - the activation log's presence fields;
  - `_hooks/exomem_retrieve_nudge.py` and its plugin mirror;
  - `benchmarks/membench/utility/context_activation*`.
- **Tool surface:** one optional object argument on one tool. The schema fidelity fixture, the tool-surface digests, the plugin and hosted trees, the ChatGPT plugin contract, `docs/capabilities.md` and the README table regenerate in S4. Remote connectors need an owner-side schema refresh, and until it happens they keep today's behaviour, because the argument is optional.
- **State:** none new. Nothing from the conversation persists beyond the call ledger's existing argument hash.
- **Pure substrate:** no model runs on conversation text. Resolution stays categorical and deterministic. The only model-authored input is the calling agent's own `focus` line, and the agent is the reasoner the constitution already names.
- **Default-off / soft-fail:** the capability is inert until a caller sends `conversation`, and hooks send it only in `working_set` mode. A malformed `conversation` is ignored and reported `absent`, never an error.
