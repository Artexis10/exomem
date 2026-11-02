## Why

A 2026-11-01 adversarial audit tested one vault that holds client matters beside personal records. Direct reads held. Two classes of leak did not:

- A page that no scope selects is open to every audience and every fenced connector. An agent that copies a matter fact into an ordinary note publishes it. An episode recap the owner records about a matter is served to every other connector by recall. A rule that restriction follows citations would not close this, because agents link without citing.
- A non-owner audience under a governed policy can create pages inside another matter, and can move its own matter's pages out of scope. A fenced connector can edit any open page it may read, so it can write client facts into shared pages. The occupied-destination refusal tells a restricted writer that a withheld page holds a name.

## What Changes

- A policy may declare one residual scope. Membership assigns it to every page and file that no other scope selects. It is an ordinary scope: `default_deny`, rules, grants and connector denied sets name it by id. With `default_deny`, unclassified pages are closed to every non-owner audience. A connector config lists it in the default denied set. The operator's personal connector opts in by leaving it out of its client entry. A vault without a residual scope keeps its exact membership.
- Every restricted writer is confined on every write. A restricted writer is a non-owner principal under a non-empty policy, or the owner through a connector whose denied set is not empty. Its write authority comes only from its client entry's new `writable_scope_ids`. It creates only in a capture inbox that its writable scopes select. It edits, moves and deletes only pages whose scope sets lie within its writable scopes, before and after the write. Every refusal is `WRITE_REFUSED: target is unavailable`, decided before any existence check.
- Writable scopes must be exclusive: denied to the default context and to every other fenced client. Each inbox is therefore private to its connector.
- A restricted writer's Source captures, episode recaps and notes land in its inbox, so it can read them back. Recaps the owner records stay unclassified and take the residual scope.

## Capabilities

### Modified Capabilities

- `governance-kernel`: a residual scope selects what no other scope selects.
- `release-gate`: restricted writers are confined to their writable scopes and inbox; write doors refuse a withheld, an absent and a read-only target with one answer.
- `episode-compilation` (active change `close-memory-loop`): a restricted writer's recap lands in its inbox.

## Impact

- Code: `src/exomem/governance/{policy,membership,connector_boundary}.py`, `src/exomem/vocabulary/instances.py`, `src/exomem/{add,move_file,delete_file,episode_memory,working_set,working_set_heat,context_catalogue,dreamer_families}.py`, and the scaffold skill.
- Behaviour on upgrade: nothing changes until a vault declares a residual scope or a connector config carries `writable_scope_ids`. Under a connector config, fenced connectors lose write-down into open pages and creation in an inbox that no writable scope selects. Non-owner audiences without a client entry become read-only.
- Downgrade: an older binary refuses the `residual` field and blocks the policy, which fails closed.
- Undo: remove the residual scope document and the `writable_scope_ids` entries.
