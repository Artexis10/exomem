## Why

Exomem's governed review, provenance, supersession, typed-relation, and corpus-activation machinery is now stronger than its human product surface: the user must operate the differentiating daily loop through CLI or agent tool calls. A local-first Epistemic Review Studio makes that advantage visible and usable without turning Exomem into a generic notes editor or hosted collaboration suite.

## What Changes

- Add a browser-based, local-first Review Studio served by the existing Exomem service, with no separate backend or required build-time service.
- Present the Epistemic Inbox and opt-in corpus-activation queue as ranked, filterable worklists with exact measurement reasons, state counts, and stable `exomem://review/<id>` identity.
- Add a review-item workspace that composes the target page, related pages, source/evidence provenance, graph neighborhood, supersession history, and current review decision into one bounded response.
- Route explicit review actions through existing governed commands: inspect/read, compile, connect, supersede, dismiss, snooze, and reopen. Agent/model suggestions remain provisional and are never accepted or written automatically.
- Add a belief-evolution view over existing history, supersession, provenance, and typed relations; the view reports recorded change and never invents a narrative or confidence score.
- Package the static UI with the Python distribution and expose a single local entry URL plus health/error states suitable for desktop and remote personal deployments.
- Keep the first release single-user and review-centered. Rich note editing, shared workspaces, real-time collaboration, cloud sync, and a generic graph canvas are explicitly out of scope.

## Capabilities

### New Capabilities

- `epistemic-review-studio`: The packaged browser UI, ranked review worklists, bounded review-item workspace, explicit governed actions, and belief-evolution visualization.
- `review-item-context`: A deterministic product command that composes the bounded data needed to inspect one stable review item consistently across MCP, REST, and CLI.

### Modified Capabilities

None. The Studio consumes the existing Inbox, activation, evolution, graph, provenance, and governed-write contracts without changing their requirements.

## Impact

- Adds packaged static web assets and authenticated Studio routes to the existing FastMCP/Starlette service.
- Adds one read-only registry command for review-item context; all write actions continue through existing commands such as `triage_memory`, `edit_memory`, `replace_memory`, and `connect_memory`.
- Reuses `exomem://` references, review state, evolution, provenance, graph context, and read APIs instead of duplicating their logic.
- Depends on the existing-corpus activation change landing first; the Studio consumes that read-only mode but does not reimplement it.
- Adds UI-focused route, asset, accessibility, and browser workflow tests. It adds no server-side reasoning model, background worker, database, or mandatory heavy dependency.
