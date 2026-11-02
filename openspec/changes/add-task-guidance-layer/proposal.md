## Why

Maps-mode activation selects context by what the turn is about. Two recorded misses, the later on 2026-11-01, lacked a different kind of context: how this kind of task must be done, and how it went wrong before. Nothing in Exomem maps a kind of task to its method or to its failure precedents, and a method note rarely shares the words of the turn that needs it. The maps packet also never says that it pre-selected nothing task-specific.

Three more gaps make the misses hard to prevent and impossible to trace:

- Agents mostly skip optional second calls, so what an agent needs to start well must arrive in the first `recall_context` response.
- The per-state moves (for example, "call again with `anchor`") live only in the retrieve hook. A client without the hook never sees them, and the same rules exist in several copies.
- No record keeps what an activation served, so no incident can name its delivered context and nobody can tell which guidance helps.

## What Changes

One maps response carries three layers with separate budgets: an always-on stance block, the unchanged map, and a task layer made of the turn slice plus guidance refs for the activity that the client declares. The first increment is maps mode only.

- **Activities registry.** A new vocabulary registry, subject `activities`, with an empty shipped pack and a vault overlay in `_Schema/activities.yaml`. Pages and rich units declare `activities`. Workflow-contract `scope.activities` is validated against the registry on save.
- **Guidance roles registry.** A registry, subject `guidance-roles`, that ships `method`, `precedent` and `failure`. Code branches only on the closed `family` attribute (`method`, `case`).
- **Stance block.** Every maps packet carries owner-marked stance lines and the activity index, each under a hard character cap with an overflow count. It is computed over the caller's view and cached by generation and by the next date boundary that any line crosses.
- **Task layer.** `conversation.activities` declares up to four keys; unknown keys are counted, never refused. The `guidance` block serves refs with `why` {activity, entity, origin} inside the slice reserve. Only owner-written or owner-reviewed declarations are served.
- **Guidance states.** The closed set `selected`, `none_reviewed`, `unknown_activity` and `none_declared`, plus `unavailable` on failure. A withheld item counts as absent.
- **Scope rule.** Scope is global, activity, entity, or activity and entity. Entity scope is an `entity`-family relation. An item is served only when every dimension it declares matches the declaration, over the caller's view.
- **Typed currency.** Units gain typed `as_of` and `until` keys and reuse `check_by`. Superseded or archived units and units past `until` are history and never guidance. Units past `check_by` carry `review_due`.
- **Served-context record.** One append-only SQLite table beside heat, one row per maps activation, the row id in the response after the cache, 30-day retention under a row cap, and an owner read-out command. It adds no automatic learning.
- **`next_action`.** Every `recall_context` and `search_memory` response carries one move, `{state, say, tool?, args?}`, from one server state table with a fixed precedence. `task_refresh` gains a closed `refresh_reason`. The hook, the server instructions, the tool descriptions and the skill shrink to "Follow `next_action` when present."
- **Correction route.** The client model judges a behavioural correction, proposes the exact stance line or precedent with its source, and writes it after the user confirms. It adds skill and bootstrap text only, and no server mechanism.
- **Text.** The check sentence, the Sources sentence for an empty slice, and the sentences that say maps never pre-select task context, ask the agent to declare the activity, to read the named guidance and to name what shaped the answer.
- **Rename.** The map item field `activity` becomes `updated`. **BREAKING** for a client that reads map items by the old name.

## Gate

Step 1 is a running measurement on a real-model harness with paired cases. Each item below is written in full and is kept or cut by its rule when the results land. The arms are:

- the stub arm, which serves hand-built guidance refs in the planned packet shape; the ablation arm, which changes only client text; the stance-only arm, which serves only a stance block; "Stub + check", which adds the check sentence to the stub arm;
- C1 and C0, across sessions: a later session gets an earlier correction as a reviewed stance line (C1) or only as an episode recap (C0), with one unrelated task as a contamination control;
- R1 and R0, within a session: the refreshed stance block is served after a correction (R1) or not (R0);
- E1 and E0, on an invented fixture with persons A and B: a precedent about A is served only when the call declares A (E1), or unscoped under the activity (E0).

- **Gate: step 1 — the whole task layer** (activities registry use in activation, guidance block, guidance states, scope rule, `conversation.activities`) ships only if the stub arm beats the ablation arm and the stance-only arm.
- **Gate: step 1 — the correction route** stays if C1 beats C0 and R1 beats R0 in most repeats, with the contamination control clean. Otherwise it shrinks to skill text, and the `correction` state and `refresh_reason = "correction"` are cut.
- **Gate: step 1 — the check sentence** stays in the `guidance_selected` move if "Stub + check" beats Stub. Otherwise that move keeps only "read the named guidance" and "name what shaped the answer".
- **Gate: step 1 — `next_action`** stays if the arms with `next_action` raise the declare rate or the guidance-read rate over the arms without it. Otherwise the per-state sentences move into the tool description, the server instructions and the skill recall loop, and the hook keeps its own lines.
- **Gate: step 1 — `conversation.entities` and the `declared` match** stay only if E0 leaks on B's task while E1 still applies on A's. Otherwise they move to "Next". The scope rule and the entity relation stay in either case.

## Capabilities

### New Capabilities

- `task-guidance`: the activities and guidance-roles registries, guidance declarations and their origin, the stance block, the declared activity, the guidance block and its states, the scope rule, guidance currency, declared entities and the rollback setting.
- `served-context-record`: the per-activation record, its row id, retention, privacy rules and owner read-out.
- `read-next-action`: the `next_action` field on read responses, the state table, `refresh_reason` and the static-surface sentence.

### Modified Capabilities

- `context-activation`: map items name their page date `updated`; an empty slice says what it does not cover; typed unit dates decide currency.
- `conversation-aware-activation`: the conversation argument gains `activities` and `entities`; resolved declarations may be recorded and listed, unresolved ones only counted.
- `workflow-contracts`: activity selectors are validated against the activities registry on save and warned on read.
- `semantic-unit-language`: `as_of`, `until`, `activities`, `guidance` and `guidance_origin` become governed, reserved unit-metadata keys.
- `context-miss-learning`: an incident links its delivered context by `served_context_id`.
- `agent-bootstrap-contract`: the activation line points at `next_action`; the capture predicate teaches the correction route.
- `retrieve-inject-hook`: working-set injection renders the stance block once per generation and the `next_action` line in place of its own state lines.

Maps mode, the turn slice, `trigger = "task_refresh"`, served-unit time and the stable-preference capture class are defined by active changes that are not yet archived (`compile-context-maps-and-profiles`, `add-maps-slice-conversation`, `close-memory-loop`, `add-activation-temporal-currency`, `capture-durable-personal-baselines`). This change states its additions to them as ADDED requirements instead of modifying them.

## Defaults and failure

- No model runs. The server validates declarations and selects by typed keys; the active agent judges the task kind, the entities and a correction. No server-side classifier, cue list or word list decides meaning.
- `EXOMEM_ACTIVATION_GUIDANCE` defaults to `on` because the layer costs indexed lookups only and is inert until a vault registers an activity or marks a stance page. `off` removes the stance and guidance blocks.
- Each part fails soft: a failed stance or guidance compile reports `unavailable` and leaves the map and the slice served; a failed record write omits `served_context_id`; a failed state table omits `next_action`.

## Impact

- Code: two vocabulary packs and their schema subjects; semantic-unit metadata parsing; the catalogue and context views (stance, guidance, rename, empty-slice description); the currency step; the conversation module; a new record module and a new state-table module; `recall_context`, `search_memory`, their CLI flags and REST routes; governed writers that stamp `guidance_origin`; workflow-contract save validation; bootstrap, server instructions, tool descriptions, the skill scaffold and the retrieve hook.
- Generated artifacts: `docs/capabilities.md`, the plugin tree and every derived copy of the skill scaffold.
- Budget: the stance block takes what it uses from `max_chars` before the map; guidance refs displace slice items inside the existing reserve; `next_action` reserves 240 characters.
- Latency: indexed lookups for stance and guidance, and one best-effort insert per maps activation after the packet is built.
- Privacy: the record holds refs, keys, counts and closed states in tenant state, never text and never in host logs; unknown keys are counts only; the activities registry is default-deny for restricted callers.
- Clients: the `activity` to `updated` rename, and the bootstrap `miss_rule` field that moves into `next_action`.
- Delivery: a feature freeze holds the opening of this change until a live install and an import fix land.

## Not Claimed

- The server does not suggest an activity or an entity; the client declares both.
- This change does not author guidance; a vault marks its own stance pages and declarations.
- It does not raise compile coverage of captured Sources.
- It does not make an agent read what it is served; step 1 and the record measure that.
