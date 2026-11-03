## Context

`recall_context` serves maps mode by default: a turn-independent map of what exists, plus a turn slice on a quarter of the budget (`TURN_SLICE_SHARE`). Nothing in the packet says how the owner wants work done, and the per-state moves reach only clients that run the retrieve hook.

Two recorded misses, the later on 2026-11-01, lacked method and failure-precedent context, not topical context. In both, the agent answered before it searched, although the skill already told it to pull first. A real-model measurement on paired cases (step 1) compared ways to put that context into the first response. A stance block gave most of the gain of pointing the agent at its method. A server-side task guidance layer did not clearly beat the stance block or a client instruction. `next_action` was the only lever that raised how often agents declared the kind of task, and no agent refreshed by itself within a session.

This change keeps what step 1 supported. It records each decision with its reasons and the alternatives rejected, and marks each gap or contradiction as `OPEN:`.

## Goals / Non-Goals

**Goals**

- A stance block in every maps response, within a fixed cap.
- One copy of every per-state move, delivered to every client in the response that it already reads.
- A record of what each activation served, joined with opens and cites.
- Typed dates that decide currency.

**Non-Goals**

- Task-specific method and precedent guidance (deferred; see "Deferred: task layer").
- Server-side suggestion of an activity or an entity.
- Authoring stance lines, or raising compile coverage of captured Sources.
- Any server-side model, classifier or check of the agent's draft.
- The stance block in anchors mode.

## Decisions

### D1. One call carries the stance block

The stance block (how the owner wants every task done), the map (what exists) and the turn slice (what the turn reached) arrive in one `recall_context` response, each with its own budget. Agents mostly skip optional second calls, and within a session they did not refresh by themselves, so context that needs a second call does not arrive.

Rejected: a separate stance tool, because agents skip it; inlining method content, because step 1 showed no gain from it.

### D2. A stance page is one page-level mark

A stance page is a page with frontmatter `stance: true`, and every unit on it, rich or compact, is a stance line. Stance lines are ordinary notes with history, sources and supersession. The mark carries no scope, because the scope rule is deferred: a stance line is a rule for every task.

Rejected: a separate stance file, because it would lose history, sources and supersession; a per-unit stance key, because compact lines cannot carry metadata.

OPEN: The first draft served only owner-written or owner-reviewed lines, through `guidance_origin`, which is deferred. Without it, the server cannot tell a line that the user confirmed from one that an agent wrote unasked. The guards in this increment are the skill's propose-then-confirm rule and the vault's governance policy over who may write a stance page.

OPEN: With no scope, a rule about one entity or one kind of task on a stance page serves on every task. The skill proposes only rules for every task as stance lines. When a scope rule ships, a scoped line on a stance page would leave the block; that change must name the narrowing.

### D3. The stance block has one cap and its own cache

The block keeps whole lines under `STANCE_MAX_CHARS` (first value 800) and counts the rest in `omitted`. It charges only the characters that it uses to `max_chars`, before the map, so a vault with no stance page loses only the block's keys. The block is cached apart from the packet. Its key covers the caller's view, the stance input generation and the next date boundary that any line crosses. Without the date boundary, a cached block would keep serving a line past its `until`.

Rejected: caching the block inside the packet, because a request with a conversation bypasses the packet cache, and the stance block does not depend on the conversation.

OPEN: The design gives a "small fixed cap" with no value. The first value, 800 characters, is chosen, not measured.

OPEN: The design computes the stance block over the caller's view and the engagement level, but it defines no effect of the level. The draft makes the block independent of the level.

### D4. Typed dates decide currency

`as_of` and `until` join `check_by` as strict ISO date keys. The currency step applies them in order: superseded or archived is history; past `until` is history with nothing written; past `check_by` stays current with `review_due`; no date stays current. `as_of` replaces the prose date as a unit's own time. The stance block withholds a line past `until` and counts it. It serves a line past `check_by` with a marker, because a date that passes with nothing happening is not evidence that a stance changed. The rules sit in `context-activation` because one currency step serves every unit. No unit carries the new keys today, so no current behaviour changes.

Rejected: a separate `review_after` key, because `check_by` already has an audit and a due projection; reading dates from prose, which is the word regex that this change stops growing.

### D5. The record is a separate append-only table

One SQLite table in the vault state directory, beside heat, holds one row per maps activation. The row names each part that the response delivered: the map by generation, the stance lines and slice items by ref, the `next_action` state and the surfaced upkeep items by queue id. It never holds text. Opens and cites stay in heat and are joined by session key and time. The row id goes into the response after the cache lookup, and a row write never fails or delays the activation.

A separate table avoids the cost of the heat ring, where each write invalidated its cache and forced a reload. The ring also keeps only 4,096 events, so it cannot be a record. Anchors mode keeps its content-free activation log; the record covers maps activations, which that log does not describe.

Rejected: extending `activations.jsonl`, which must stay content-free; writing into the heat ring, for the cost above.

OPEN: One part of the design omits logged refs in content-private hosted mode; another keeps rows in tenant state, which works in that mode. The draft keeps rows in tenant state in every mode and never in host logs.

OPEN: The design says "never serve one caller's rows to another" and also names an owner read-out. The draft lets only the owner read-out read across principals, because the owner owns the vault and delegated agents act for the owner.

OPEN: The read-out surface is unnamed. The draft puts it on the CLI and on the owner's REST facade over one leaf, with no MCP tool. A hosted owner without a CLI uses the REST facade.

OPEN: The slice change says that a maps activation writes no session, heat or learning record, and the conversation contract keeps conversation content request-scoped. The record stores slice refs, and the conversation reached some of them. The draft treats a served slice ref as served vault material, not as conversation content, and records no conversation field. Whichever change archives second must reconcile the wording.

### D6. `next_action` is the only copy of per-state moves

Every `recall_context` and `search_memory` response carries one move, `{state, say, tool?, args?}`, chosen from one state table by a fixed precedence: `ambiguous`, `review_due`, `slice_empty`; and `search_miss` for search. The precedence puts ambiguity first, because the agent cannot proceed without a choice. The review marker comes next, because it changes how the agent applies a line that it already holds. The empty-slice move comes last. The field reuses the write terminal's name and widens its shape additively. Every named call comes from `capabilities.active_call_text`, so a move never names a call that the caller cannot make. The hook, the server instructions, the tool descriptions and the skill carry only "Follow `next_action` when present."

Rejected: keeping per-state text in each surface, which is how the rules drifted apart; branching on the client's name; a state for a turn that the client has not refreshed. Without the task layer, the server has no signal for such a turn: it never sees the conversation between calls, and the moves that asked for a refresh belonged to the task layer or to the correction state. A time or call-count threshold would guess at what the turn needs.

OPEN: The first increment is maps mode only, but the hook's `anchor` line must come from one source in both modes. The draft lets the `ambiguous` state cover anchors-mode abstentions too.

OPEN: The design takes `say` from the packet budget and also adds the field after the packet cache. The draft reserves a fixed 240 characters of `max_chars` in every maps packet, so the response stays within `max_chars` and the cached packet does not depend on the state.

OPEN: The bootstrap `miss_rule` moves into the `search_miss` move. The active `shrink-bootstrap` change edits the same payload; the two changes must agree on the field's removal.

OPEN: The hook drops its own per-state lines. A hook that talks to a server without `next_action` then renders no `anchor` line for an ambiguous packet. The draft accepts that skew, because the hook and the service ship in one package.

### D7. Corrections stay with the client model, as skill text

The client model decides whether a statement corrects how the agent behaves on every task. The agent proposes the exact stance line with the user's words as its source, and writes it after the user confirms. A later line replaces an earlier one through `supersedes`. A changed stance generation makes the hook show the block again, so later turns serve the line without a refresh. The server adds no detector, no refresh reason and no correction state.

Rejected: correction cues in `context-roles.yaml` or in code, which are word lists that decide meaning; a `correction` move, which step 1 did not support.

### D8. `activity` on map items becomes `updated`

"Activity" already means a page's last update date on map items, recent assistant activity in bootstrap, and a kind of task in workflow contracts. The served unit already calls the same date `updated`, and `as_of` is served under that name. So the map item field is renamed to `updated`.

### D9. Additions to unarchived changes are ADDED requirements

Maps mode, the turn slice, served-unit time and the stable-preference capture class are defined by active changes that are not archived. Following the precedent of `declare-topic-carriers`, this change states its additions as ADDED requirements in `stance-block`, `read-next-action`, `context-activation`, `agent-bootstrap-contract` and `retrieve-inject-hook`.

OPEN: Two ADDED requirements change what an unarchived change says. Past-`until` history widens "History comes only from an authored supersession" in `add-activation-temporal-currency`: an `until` is authored, but it is not a supersession. The `updated` rename changes the "entry activity" wording in `compile-context-maps-and-profiles`. Whichever change archives second must refresh the other's text.

## Deferred: task layer

Step 1 did not support the task layer. Each piece below moves to a second increment, gated on a larger development round on fresh cases, in which the guidance arm beats both the stance-only arm and a client-instruction arm:

- the activities registry (subject `activities`), with its empty shipped pack, its overlay and its default-deny admission;
- the guidance-roles registry (subject `guidance-roles`) and its closed `family` attribute;
- the guidance declarations on pages and rich units (`activities`, `guidance`), and the `guidance_origin` key with its serving rule;
- `conversation.activities`, and the activity index in the stance block;
- the guidance block, its `why` and its states, and the `next_action` moves that point at them;
- the scope rule over activity and entity, read through the `entity` relation family;
- the currency of guidance items;
- `conversation.entities` and the slice match value `declared`;
- the validation of workflow-contract activity selectors against the activities registry.

Server-suggested activities and entities come after the second increment, and only if clients do not declare.

## C4 and C6 review

- **C4 (vocabulary fixed in code): absent in the change.** The change adds no category set. `stance` is a boolean page key, and the record, the stance block and `next_action` carry refs, generations and closed states.
- **Closed sets are protocol tokens that this specification fixes**, which is sound: the stance states, the `next_action` states and the `stance` values.
- **C6 (word lists and regexes deciding meaning): absent in the change.** The client model judges a correction. Dates are typed keys with a format check.
- **Old debt, reported and not rewritten:**
  - `_DEADLINE_CUE` in `working_set_currency.py` is an English word regex that decides whether a prose date is a deadline. With `as_of`, it stops growing and retires with the prose fallback.
  - The sixteen core semantic categories are defined in code in `semantic_language_registry.py`.
  - The `context-roles.yaml` cue lists are vault data, but they are word lists that decide meaning.

## Costs

- **Latency.** Stance: a cached read; a cache miss reads the stance pages' units through the index. Record: one best-effort insert after the packet is built, with a short busy timeout. `next_action`: a table lookup. None of these is measured yet; task 11.1 measures activation p95.
- **Tokens.** The stance block takes up to 800 characters of `max_chars` when stance pages exist, and only its keys otherwise. `next_action` reserves 240 characters. `served_context_id` adds about 20 characters outside the cached packet.
- **Privacy.** The stance block is computed over the caller's view; withheld pages count as absent. The record holds refs, generations and closed states in tenant state, records each row under its caller and never writes host logs.
- **Maintenance.** One page mark, one table with one retention sweep, one read-out, and one module of move text that replaces several copies. The owner curates the stance pages.

## Risks

- Agents may not follow the kept moves. Step 1 measured `next_action` on the declaration move, which this change defers. The record and the first development run measure the kept moves.
- An agent may write a stance line that the user did not mean. Propose-then-confirm is the only guard in this increment (D2), and the line serves on every task until the owner removes it.
- Many lines may press on the stance cap. The block counts overflow, and the owner curates the lines.
- The `updated` rename breaks a client that reads map items by the old name. The rename ships with the stance block and is named in the release notes.

## Proof plan

The workflow that this change alters is the first `recall_context` call of a task and every hook packet that follows it. The highest-level check is the first development run of the built change on the step-1 harness with real models. Per case, it reports the required items used against the default map, and whether the agent follows each move that `next_action` serves. It also compares the served-context record against the harness transcript. Behaviour that depends on the client model (following a move, proposing and confirming a stance line) is proven only on real models; stubbed models prove wiring.

## Migration and rollout

1. Ship the date keys, the stance mark and the record, so nothing changes for a vault without stance pages or typed dates.
2. Ship the stance block and `next_action` together, so the static surfaces shrink in the same release that adds the moves.
3. Roll back the stance block with `EXOMEM_ACTIVATION_STANCE=off`; the record and `next_action` keep working.

## Next

- The second increment (see "Deferred: task layer").
- A default-off dreamer family that reads the record and proposes owner-reviewed changes.
- A background compiler as its own change: a scheduled agent client that writes through governed routes, gated by the offline reach round and by its review-minutes figure.
- Compile coverage of captured Sources, gated by its offline reach round.

## Dropped

- Entity cards and temporal validity as separate pieces: anchor profiles and unit currency already provide them.
- A server-side check of the agent's draft: the server never sees the draft, and judging it is model judgment.
- The check sentence: step 1 showed no gain from it.
- `refresh_reason`: its only effect was the `correction` move, which step 1 cut.
- A `conversation.boundaries` input: the client already holds its conversation.
- A counted Source signal ("N Sources match"), until compile coverage is measured.
- A generative model inside the server for any of these pieces.
