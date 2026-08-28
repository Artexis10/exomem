## Why

Exomem's shipped governance plane still has several deliberate escape hatches that are tolerable while confidential vaults remain physically isolated but unsafe once one item can belong to both personal and delegated-client compartments. Those gaps must close before a vault-consolidation workflow can honestly replace physical isolation with policy enforcement.

## What Changes

- Make standing and session grants compose per matched scope so a grant for one scope cannot lift a second closed scope on the same item.
- Project or omit opt-in raw `get` content below full disclosure so the response cannot contradict its own L5 ceiling or provenance stripping.
- Reserve governance and consolidation administration trees from every generic create, edit, append, move, delete, recover, dataset, media, and alias route; policy remains authorable only through the reviewed governance lifecycle.
- Bind prospective policy compilation to the same live conflict state and fingerprint that commit validates.
- Bind explicit authorization-session handles to the resolved request principal at generated MCP, REST, Hosted, and CLI boundaries without allowing caller-selected identity.
- Remove pre-filter rank, graph-degree, count, error-shape, and avoidable corpus-size timing channels from withheld-item responses.
- Make non-Markdown compartment membership explicit and fail closed when semantic selectors cannot classify an artifact.
- Sync the already-shipped default-deny behavior into the canonical governance contract and close the stale Wave 0/1 OpenSpec bookkeeping.

## Capabilities

### New Capabilities

- `authorization-session-binding`: Define trusted, surface-specific authorization-session identity and reject caller-selected or cross-session grant/declaration authority.

### Modified Capabilities

- `governance-kernel`: Make default-deny canonical, compose grants per scope, bind prospective compiles to live conflict state, and fail closed for unclassifiable non-Markdown artifacts.
- `release-gate`: Remove raw, ranking, graph, error, count, and avoidable timing disclosure channels and reserve administration state from all public projections.
- `get-payload-shape`: Make `include_raw` obey the effective release level and provenance projection.
- `governance-authoring`: Give reviewed governance authoring exclusive mutation authority over `_Governance/` and bind authorization sessions to resolved principals.
- `command-surface`: Thread authorization-session context safely and classify reserved administration paths consistently across MCP, REST, Hosted, and CLI.

## Impact

- Affected code centres on `governance/decisions.py`, `governance/policy.py`, `governance/egress.py`, `governance/principal.py`, `governance/tool.py`, the command registry and generated surface adapters, path/mutation guards, membership evaluation, and their tests.
- **BREAKING**: generic filesystem operations that previously reached `_Governance/` or `_Consolidation/` will refuse and direct callers to the governed product command.
- **BREAKING**: `include_raw=true` no longer returns exact stored bytes to an audience below L6.
- Existing ungoverned-vault behavior remains baseline-open plus the terminal secret scrubber; no model or optional heavy dependency is introduced.
