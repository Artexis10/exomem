## Why

Maps-mode activation selects context by what the turn is about. Two recorded misses, the later on 2026-11-01, lacked a different kind of context: how the owner wants this kind of task done, and how it went wrong before. A real-model measurement on paired cases (step 1) then tested what helps an agent start a task well. It showed:

- Pointing the agent at its method roughly doubled the required items that its answers used, against the default map.
- A stance block alone gave most of that gain.
- Inlining the method content did not help.
- A server-side task guidance layer did not clearly beat a stance block or a client instruction.
- `next_action` was the only lever that raised how often agents declared the kind of task.
- Within a session, an agent never refreshed its context by itself. Steering must arrive on the response that the client already reads.

Two more gaps make misses hard to prevent and impossible to trace:

- The per-state moves (for example, "call again with `anchor`") live only in the retrieve hook. A client without the hook never sees them, and the same rules exist in several copies.
- No record keeps what an activation served, so no incident can name its delivered context.

## What Changes

One maps response carries an always-on stance block beside the unchanged map and turn slice, and every read response carries its next move. The first increment is maps mode only.

- **Stance block.** A page marks itself a stance page with `stance: true`. Every maps packet carries the stance page lines under one hard character cap with an overflow count. The block is computed over the caller's view, and it is cached by generation and by the next date boundary that any line crosses. `EXOMEM_ACTIVATION_STANCE=off` removes it.
- **Typed currency.** Units gain typed `as_of` and `until` keys and reuse `check_by`. A superseded or archived unit and a unit past `until` are history. A unit past `check_by` carries `review_due`. The stance block withholds expired lines and counts them.
- **Served-context record.** One append-only SQLite table beside heat holds one row per maps activation: refs, generations and closed states, never text. The response carries the row id after the cache. Rows expire after 30 days or above a row cap, and the owner reads them through one command. The record adds no automatic learning.
- **`next_action`.** Every `recall_context` and `search_memory` response carries one move, `{state, say, tool?, args?}`, from one server state table with a fixed precedence: `ambiguous`, `review_due`, `slice_empty`, and `search_miss` for search. The hook, the server instructions, the tool descriptions and the skill shrink to "Follow `next_action` when present."
- **Stance-line route.** The skill and bootstrap teach the agent to propose a stance line with its source and to write it after the user confirms. It adds no server mechanism.
- **Sources sentence.** An empty turn slice says that the map and the slice cover compiled material only, and that `search_memory` reaches captured Sources.
- **Rename.** The map item field `activity` becomes `updated`. **BREAKING** for a client that reads map items by the old name.

## Gate

Step 1 decided each gated item of the first draft:

- **The task layer: not met.** The activities and guidance-roles registries, guidance declarations, the declared activity and entities, the guidance block, the scope rule and guidance currency move to a second increment. design.md lists each deferred piece and its gate.
- **The check sentence: not met.** It is cut.
- **The correction route: shrinks to skill text.** `refresh_reason` and the `correction` state are cut.
- **`next_action`: met in direction.** It stays, with a state table that covers only the states that this change serves.

## Capabilities

### New Capabilities

- `stance-block`: the stance page mark, the stance block, its cache, the currency of stance lines and the rollback setting.
- `served-context-record`: the per-activation record, its row id, retention, privacy rules and owner read-out.
- `read-next-action`: the `next_action` field on read responses, the state table and the static-surface sentence.

### Modified Capabilities

- `context-activation`: map items name their page date `updated`; an empty slice says what it does not cover; typed unit dates decide currency.
- `semantic-unit-language`: `as_of` and `until` become governed, reserved unit-metadata keys.
- `context-miss-learning`: an incident links its delivered context by `served_context_id`.
- `agent-bootstrap-contract`: the activation line points at `next_action`; the capture axis teaches the stance-line route.
- `retrieve-inject-hook`: working-set injection renders the stance block once per generation, and the `next_action` line in place of its own state lines.

Maps mode, the turn slice, served-unit time and the stable-preference capture class are defined by active changes that are not yet archived (`compile-context-maps-and-profiles`, `add-maps-slice-conversation`, `close-memory-loop`, `add-activation-temporal-currency`, `capture-durable-personal-baselines`). This change states its additions to them as ADDED requirements instead of modifying them.

## Defaults and failure

- No model runs. The client model judges a correction; the server reads typed keys only. No server-side classifier, cue list or word list decides meaning.
- `EXOMEM_ACTIVATION_STANCE` defaults to `on`, because the block costs one cached read and serves nothing until a vault marks a stance page.
- Each part fails soft: a failed stance compile reports `unavailable` and leaves the map and the slice served; a failed record write omits `served_context_id`; a failed state table omits `next_action`. Each failure is named in diagnostics.

## Impact

- Code: the `stance` frontmatter key and the two date keys; the catalogue and context views (stance block, rename, empty-slice description); the currency step; a new record module and a new state-table module; `recall_context`, `search_memory`, the record read-out command and their REST routes; bootstrap, server instructions, tool descriptions, the skill scaffold and the retrieve hook.
- Generated artifacts: `docs/capabilities.md`, the plugin tree and every derived copy of the skill scaffold.
- Budget: the stance block takes what it uses from `max_chars` before the map, at most `STANCE_MAX_CHARS`; `next_action` reserves 240 characters.
- Latency: one cached read for the stance block, and one best-effort insert per maps activation after the packet is built.
- Privacy: the record holds refs, generations and closed states in tenant state, never text and never in host logs.
- Clients: the `activity` to `updated` rename, and the bootstrap `miss_rule` field that moves into `next_action`.
- Delivery: a feature freeze holds the opening of this change until a live install and an import fix land.

## Not Claimed

- This change serves no task-specific method or precedent guidance; that is the deferred second increment.
- It does not author stance lines; the owner confirms each one, and a vault marks its own stance pages.
- It does not raise compile coverage of captured Sources.
- Step 1 showed that `next_action` raised how often agents declared the kind of task, a move that this change defers. It did not measure the moves that this change keeps. This change does not claim that agents follow them; the record and the first development run measure that.
- It does not make an agent read what it is served.
