## Context

`recall_context` serves maps mode by default: a turn-independent map of what exists, plus a turn slice on a quarter of the budget (`TURN_SLICE_SHARE`). Anchors mode, with its role lenses for methods and precedents, is a rollback that live services do not run. So no agent receives method or precedent context today, and nothing maps a kind of task to it. Workflow contracts already name activities in `scope.activities`, but they are not served through activation.

Two recorded misses, the later on 2026-11-01, lacked method and failure-precedent context, not topical context. In both, the agent answered before it searched, although the skill already told it to pull first. The design therefore puts what an agent needs into the first response, keeps the decision of the task kind with the client model, and measures whether clients use what they are given (step 1) before the change ships.

The design is settled. This document records its decisions, the reasons and the alternatives rejected, and marks each gap or contradiction found while drafting as `OPEN:`.

## Goals / Non-Goals

**Goals**

- One maps response with three budgeted layers: stance, map, task.
- Task-specific method and precedent selection by a declared activity, not by topic words.
- Vocabulary in registries, and code that branches only on closed attributes.
- A record of what each activation served, joined with opens and cites.
- One copy of every per-state move, delivered to every client in the response.

**Non-Goals**

- Server-side suggestion of an activity or an entity.
- Authoring guidance, or raising compile coverage of captured Sources.
- Any server-side model, classifier or check of the agent's draft.
- Anchors-mode guidance.

## Decisions

### D1. One call carries three layers

The stance block (who the owner is: stance lines and the activity index), the map (what exists) and the task layer (the slice plus guidance refs) arrive in one `recall_context` response, each with its own budget. Agents mostly skip optional second calls, so a design that needs a second call to reach method context fails in the same way as today.

Rejected: separate tools for stance and guidance, because agents skip them; pinning all guidance in every response, because it costs tokens on unrelated turns and loses the selection.

### D2. Activities are a registry with an empty shipped pack

Activities follow the vocabulary-registry pattern: a pack under `src/exomem/vocabulary/packs/core/`, a vault overlay in `_Schema/`, `schema_memory` as the governed writer, and default-deny admission for restricted callers, like workflow contracts. The pack ships empty because a task vocabulary is the owner's, not the product's. Keys reuse the workflow-contract activity-selector grammar, so one key names one kind of task in both places.

Rejected: activities as pages, because a key fits a field on many items and a page does not; folder or tag carriers, because they come later as upkeep proposals; a shipped starter list, because it would be vocabulary fixed by the product.

### D3. Guidance roles are a registry with a closed `family`

`method`, `precedent` and `failure` ship in a `guidance-roles` pack; an overlay may add roles. Each role declares `family`, a closed set (`method`, `case`) that the spec fixes. Code orders the guidance block by family and never names a role key.

Rejected: reusing `context-roles.yaml`, because those roles are retrieval lanes chosen by anchor kind and substring cue phrases, and this change must not extend those cue lists; reusing note types, because a role belongs to a unit as well as a page, and the `failure` note type describes a page's form, not its use for a task.

### D4. Pages and rich units declare scope; nothing inherits

A guidance item is a page (frontmatter) or a rich unit (governed metadata rows) that declares `activities` or a `guidance` role, or a scoped page or unit on a stance page. An `entity`-family relation narrows its scope. A page declaration does not pass to its units, and a unit declaration does not pass to its page. Inheritance would make one frontmatter edit change what many units serve, which the owner cannot see in the unit.

A stance page is a page with `stance: true`. Its unscoped lines enter the stance block. A scoped stance page, or a scoped rich unit on a stance page, is a guidance item instead. Compact lines cannot carry metadata: on an unscoped stance page they enter the stance block with the page's origin, and on a scoped stance page they stay out of the block because the page itself is the guidance item.

Rejected: a separate stance file, because stance lines are ordinary notes with history, sources and supersession.

### D5. Origin decides serving

Only owner-written or owner-reviewed declarations are served. A declaration records `guidance_origin`: `inferred` or `reviewed`. A declaration without an origin was written by hand and reads as `authored`. Governed writes stamp `inferred` unless an owner principal passes the owner's confirmation. A nonowner principal can record only `inferred`. This mirrors the topic-carrier origin rule, where a declaration without a recorded origin reads as reviewed.

Rejected: serving inferred declarations with a label, because an agent that writes a stance the user did not mean would then steer every later turn.

OPEN: The design states the serving rule but not how the server records owner review. The draft uses the `guidance_origin` key above and trusts the owner principal's agent to pass the confirmation after the user confirms in the conversation.

OPEN: The design names `why.origin` but not its values. The draft serves `authored` (no recorded origin) or `reviewed`.

### D6. The stance block has two capped parts and its own cache

The block holds stance lines and the activity index, each under a named cap: `STANCE_LINES_MAX_CHARS` (first value 800) and `STANCE_INDEX_MAX_CHARS` (first value 400). Each part keeps whole items and counts the rest. The block charges only the characters it uses to `max_chars`, before the map, so a vault with no stance page loses nothing. The block is cached apart from the packet: its key covers the caller's view, the caller's registry hash, the stance input generation and the next date boundary that any line crosses. Without the date boundary, a cached block would keep serving a line past its `until`.

Rejected: one shared cap, because a long index would starve the stance lines; caching the block inside the packet, because a request with a conversation bypasses the packet cache and the stance block does not depend on the conversation.

OPEN: The design gives one "small fixed cap". The draft uses two caps, with first values chosen here, not measured.

OPEN: One part of the design lists each activity's guidance refs in the activity index; the change's scope does not. The draft lists only key and description, and serves guidance refs only for a declared activity.

OPEN: The design computes the stance block over the caller's view and the engagement level, but defines no effect of the level. The draft makes the block independent of the level.

### D7. The client declares the activity on each call

`conversation.activities` carries at most four keys. The server resolves each against the caller's view of the registry and counts every unknown, malformed, surplus or withheld key, never echoing it. The declaration is request-scoped: the continuity token must not encode a conversation field, so a later call declares again. The retrieve hook sends no declaration, so a hook packet always reports `none_declared`, and the `next_action` move asks the agent for a task refresh that declares the activity.

Rejected: carrying the declaration in the continuity token, which the conversation contract forbids; refusing an unknown key, because the argument never refuses and a refusal costs a turn.

### D8. Guidance refs ride in the slice reserve

The guidance block serves `{ref, title, role, why}` and no item text; the agent reads an item with `read_memory`. Guidance items take the slice reserve first and slice items fill the rest, so the map budget is unchanged. Order is by scope (activity and entity, entity, activity), then family, then ref.

OPEN: One part of the design serves guidance content through an `exomem://guidance/<key>` view; the change's scope does not include it. The draft leaves it out; `read_memory` reads each ref.

### D9. Guidance states are closed, and failure is visible

`selected`, `none_reviewed`, `unknown_activity` and `none_declared` follow the design. A withheld item, an inferred item and an item that currency makes history each count as absent, so a restricted caller's state equals the state of a vault without the item.

OPEN: The design fixes four states and none for a failed lookup. The house rule says a failed read is a visible state, never an empty one. The draft adds `unavailable`, which mirrors the slice state of the same name.

### D10. Scope reads closed families, never keys

Scope takes four forms: global (the stance block), activity, entity, and activity and entity. Entity scope is a relation of family `entity` to a page whose anchor kind is `entity`. Code reads only the closed family and the closed anchor kind, as currency reads the `supersession` family. An item is selected only when every dimension it declares matches the client's declaration, over the caller's view. So an entity-scoped precedent never serves on another entity's task or on a task that declares no entity.

Without `conversation.entities` (gated), no client can declare an entity, so entity-scoped items stay unserved. The scope rule ships anyway, because adding it later would narrow guidance that was already served unscoped. A stance page that carries a note-level `entity`-family relation becomes scoped and leaves the always-on block; that follows from D4.

OPEN: The design says that an item with no activity, no entity and no stance mark is not guidance, which implies that an entity relation alone makes an item guidance. Ordinary notes carry `about_entity` relations, so that reading would serve every note about a declared person as guidance. The draft requires `activities`, a `guidance` role or a stance page, and lets the entity relation only narrow the scope.

Rejected: scoping by entity type, which is vocabulary; the entity-type `cue_nouns` attribute, which is a word list; reusing `anchor`, which in anchors mode is a correction pick that the learning sensor counts.

### D11. Typed dates decide currency

`as_of` and `until` join `check_by` as strict ISO date keys. The currency step applies them in order: superseded or archived is history; past `until` is history with nothing written; past `check_by` stays current with `review_due`; no date stays current. `as_of` replaces the prose date as a unit's own time. The stance block withholds a line past `until` and counts it, and serves a line past `check_by` with a marker, because a date that passes with nothing happening is not evidence that a stance changed. The rules sit in `context-activation` because one currency step serves every unit; no unit carries the new keys today, so no current behaviour changes.

Rejected: a separate `review_after` key, because `check_by` already has an audit and a due projection; reading dates from prose, which is the word regex this change stops growing.

### D12. The record is a separate append-only table

One SQLite table in the vault state directory, beside heat, holds one row per maps activation: refs, keys, counts and closed states, never text. Opens and cites stay in heat and are joined by session key and time. The row id goes into the response after the cache lookup. A row write never fails or delays the activation. A separate table avoids the heat ring's cost, where each write invalidated its cache and forced a reload. The ring also keeps only 4,096 events, so it cannot be a record.

Anchors mode keeps its content-free activation log; the record covers maps activations, which that log describes as nothing.

Rejected: extending `activations.jsonl`, which must stay content-free; writing into the heat ring, for the cost above.

OPEN: One part of the design omits logged keys and refs in content-private hosted mode; another keeps rows in tenant state, which works in that mode. The draft keeps rows in tenant state in every mode and never in host logs.

OPEN: The design says "never serve one caller's rows to another" and also names an owner read-out. The draft lets only the owner read-out read across principals, because the owner owns the vault and delegated agents act for the owner.

OPEN: The read-out surface is unnamed. The draft puts it on the CLI and the owner's REST facade over one leaf, with no MCP tool. A hosted owner without a CLI uses the REST facade.

### D13. `next_action` is the only copy of per-state moves

Every `recall_context` and `search_memory` response carries one move, `{state, say, tool?, args?}`, chosen from one state table by a fixed precedence: `correction`, `ambiguous`, `guidance_selected`, `guidance_unknown_activity`, `guidance_none_declared`, `review_due`, `slice_empty`; and `search_miss` for search. The field reuses the write terminal's name and widens its shape additively. Every named call comes from `capabilities.active_call_text`, so a move never names a call the caller cannot make, which keeps the rule that no `next_action` advertises a path that no code performs. The hook, the server instructions, the tool descriptions and the skill carry only "Follow `next_action` when present." The sentences that say maps never pre-select task context, declare the activity, read the named guidance and name what shaped the answer, the check sentence and the Sources move arrive per state through this table.

The precedence puts a correction first, because saving it is the move the user just asked for in substance; then ambiguity, because the agent cannot proceed without a choice; then guidance application, then declaration repairs, then review and empty-slice moves.

OPEN: The first increment is maps mode only, but the hook's `anchor` line must come from one source in both modes. The draft lets the `ambiguous` state cover anchors-mode abstentions too.

OPEN: The design takes `say` from the packet budget and also adds the field after the packet cache. The draft reserves a fixed 240 characters of `max_chars` in every maps packet, so the response stays within `max_chars` and the cached packet does not depend on the state.

OPEN: The bootstrap `miss_rule` moves into the `search_miss` move. The active `shrink-bootstrap` change edits the same payload; the two must agree on the field's removal.

OPEN: The hook drops its own per-state lines. A hook that talks to a server without `next_action` then renders no `anchor` line for an ambiguous packet. The draft accepts that skew, because the hook and the service ship in one package.

Rejected: keeping per-state text in each surface, which is how the rules drifted apart; branching on the client's name.

### D14. `refresh_reason` is closed and never refuses

`task_refresh` accepts `new_evidence`, `task_change` or `correction`. The value selects only the `correction` move and changes no selection. An unknown or misplaced value is ignored and named in diagnostics, so a hint never costs a turn.

Rejected: refusing an invalid value, because the refresh carries the evidence the agent needs now.

### D15. Corrections stay with the client model

The client model decides whether a statement corrects behaviour and whether it is a stance line or a precedent. The agent proposes the exact unit with the user's words as its source, writes it after the user confirms, and refreshes with `refresh_reason = "correction"`. A changed stance generation makes the hook show the block again, so later turns serve the correction. Conflicts reuse `supersedes` and `contradicts`. The server adds no detector and no `conversation.boundaries` input: it would only echo text the client already holds, and it must never log that text.

Rejected: correction cues in `context-roles.yaml` or code, which are word lists deciding meaning.

### D16. `activity` on map items becomes `updated`

"Activity" already means a page's last update date on map items, recent assistant activity in bootstrap, and a kind of task in workflow contracts. This change adds `conversation.activities` and `why.activity` to the same packet, so the map item field is renamed to `updated`, which also matches the served unit's `updated`.

### D17. Workflow contracts refuse unregistered activities on save and warn on read

A save refuses an activity selector that is not a current registry key. A read of a stored contract with such a selector reports `unregistered_activity` and keeps resolving it.

OPEN: The canonical schema makes unknown values invalid on read. Applying that to registry membership would make every non-explicit resolution refuse in a vault whose contracts predate the registry, because the shipped pack is empty. The draft warns on read instead.

### D18. Additions to unarchived changes are ADDED requirements

Maps mode, the turn slice, `trigger = "task_refresh"`, served-unit time and the stable-preference capture class are defined by active changes that are not archived. Following the precedent of `declare-topic-carriers`, this change states its additions as ADDED requirements in `task-guidance`, `read-next-action`, `context-activation`, `agent-bootstrap-contract` and `retrieve-inject-hook`.

OPEN: Two ADDED requirements change what an unarchived change says. Past-`until` history widens "History comes only from an authored supersession" in `add-activation-temporal-currency` (an `until` is authored, but it is not a supersession). The `updated` rename changes the "entry activity" wording in `compile-context-maps-and-profiles`. Whichever change archives second must refresh the other's text.

OPEN: The shipped guidance-roles pack ships three entries. The design says the roles "live in a registry" but not whether the product ships them. The draft ships them, because the family attribute needs at least one role per family to be usable in a new vault.

## C4 and C6 review

- **C4 (vocabulary fixed in code): absent in the change.** Activities and guidance roles live in registries. Code branches only on closed attribute values that a schema declares: the guidance-role `family`, the relation `family` `entity` and the anchor kind `entity`. It never tests an activity key, a role key, an entity-type key or a relation key.
- **Closed sets are protocol tokens that this specification fixes**, which is sound: the guidance states, the stance states, the `next_action` states, the `refresh_reason` values, the `guidance_origin` values, the `family` values and the new slice match value `declared`.
- **C6 (word lists and regexes deciding meaning): absent in the change.** The client model judges the task kind, the entities and a correction; the owner declares scope. Dates are typed keys with a format check. The activity-key grammar checks shape only. The change does not extend the `context-roles.yaml` cue lists and does not read the entity-type `cue_nouns` attribute.
- **Old debt, reported and not rewritten:**
  - `_DEADLINE_CUE` in `working_set_currency.py` is an English word regex that decides whether a prose date is a deadline. With `as_of`, it stops growing and retires with the prose fallback.
  - The sixteen core semantic categories are defined in code in `semantic_language_registry.py`.
  - The `context-roles.yaml` cue lists are vault data, but they are word lists that decide meaning.

## Costs

- **Latency.** Stance: a cached read; a cache miss reads the stance pages' units through the index. Guidance: one indexed lookup by declared key over the declaration index. Record: one best-effort insert after the packet is built, with a short busy timeout. `next_action`: a table lookup. None of these is measured yet; task 16.1 measures activation p95.
- **Tokens.** The stance block takes up to 1,200 characters of `max_chars` when stance pages and activities exist, and nothing else otherwise. Guidance refs displace slice items inside the existing reserve. `next_action` reserves 240 characters. `served_context_id` adds about 20 characters outside the cached packet.
- **Privacy.** Every block is computed over the caller's view; withheld items and entities count as absent. The activities registry is default-deny for restricted callers. The record holds refs, keys, counts and closed states in tenant state, records each row under its caller, never writes host logs, and counts unknown keys without their values.
- **Maintenance.** Two registries, one table with one retention sweep, one read-out, and one module of move text that replaces several copies. The owner carries a curation cost: notes must declare activities and stance marks before anything is served. Compile-time authoring, where capture and curation propose unit roles and activity links for owner review, and upkeep proposals are the planned relief.

## Risks

- Clients may not declare, or may not read what they are served. Step 1 measures both before the change ships, and the record measures them in live use.
- An agent may write a stance the user did not mean. Propose-then-confirm and the reviewed-only serving rule bound the harm to review time.
- Many corrections may press on the stance cap. The block counts overflow and the owner curates the lines.
- The `updated` rename breaks a client that reads map items by the old name. The rename ships in the same release as the new fields and is named in the release notes.

## Proof plan

The workflow this change alters is the first `recall_context` call of a task and its refresh. The highest-level check is the first development run of the built change on the step-1 harness with real models (step 2): per case, the declare rate, the guidance-read rate and the required items used, with the served-context record compared against the harness transcript. Behaviour that depends on the client model (declaring, reading, applying, correcting) is proven only on real models; stubbed models prove wiring.

## Migration and rollout

1. Ship the registries, keys and record with an empty pack, so nothing changes for a vault that declares nothing.
2. Ship the stance and guidance blocks and `next_action` together, so the static surfaces shrink in the same release that adds the moves.
3. Roll back the task layer with `EXOMEM_ACTIVATION_GUIDANCE=off`; the record and `next_action` keep working.

## Next

- A default-off dreamer family that reads the record and proposes owner-reviewed changes: drop a declaration that is served often and never opened; declare a unit for an activity under which agents open it often.
- Server-suggested activities and entities, by embedding similarity only, built only if step 1's conditional arm shows that clients do not declare. A suggested entity never unlocks entity-scoped guidance.
- A background compiler as its own change: a scheduled agent client that writes through governed routes, gated by the offline reach round and by its review-minutes figure.
- Compile coverage of captured Sources, gated by its offline reach round.
- `conversation.entities` and the `declared` match, if their step-1 gate fails.

## Dropped

- Entity cards and temporal validity as separate pieces: anchor profiles and unit currency already provide them.
- A server-side check of the agent's draft: the server never sees the draft, and judging it is model judgment.
- A `conversation.boundaries` input: the client already holds its conversation.
- A counted Source signal ("N Sources match"), until compile coverage is measured.
- A generative model inside the server for any of these pieces.
