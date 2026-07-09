## Context

Exomem's command registry is now the correct source of truth: MCP, REST, CLI, docs, and OpenAPI derive from the same leaf functions. That architecture should not be replaced. The problem is product shape: the canonical tools expose Exomem's internal architecture directly, so first-use agents and humans see a broad technical menu before they understand the few common actions.

The latest main includes adoption, knowledge packs, semantic blocks, compile planning, agent memory boundary docs, and the epistemic graph sidecar. The next gap is the front door: "ask", "remember", "capture", "review", "connect", "adopt", and "maintain" should be obvious before users learn `find`, `note`, `add`, `propose_compilation`, `graph_context`, or `audit_fix`.

## Goals / Non-Goals

**Goals:**

- Add a simple action layer that maps common user intent to canonical registry commands.
- Keep the registry as the only operation contract; simple actions are aliases/routing metadata, not duplicate implementations.
- Make CLI use easier without hiding advanced commands from power users.
- Make bootstrap and scaffold docs teach actions first and tools second.
- Preserve MCP schema stability unless a canonical tool intentionally changes.

**Non-Goals:**

- Do not remove, rename, or break existing registry commands.
- Do not add server-side reasoning or LLM selection.
- Do not make graph enrichment, model-backed relation suggestions, or repair operations automatic.
- Do not replace existing REST endpoints or OpenAPI paths.
- Do not copy Basic Memory's implementation or syntax.

## Decisions

1. Simple actions are metadata plus thin dispatch, not new storage primitives.

   Rationale: Exomem's moat depends on governed layers, but those layers already exist behind canonical commands. A new action model should choose the right existing tool and defaults, not create another source of truth.

   Alternative considered: add independent `remember`, `ask`, and `review` operations with their own logic. Rejected because it would duplicate validation, drift from MCP/REST/CLI parity, and blur the pure-substrate boundary.

2. CLI aliases are allowed; MCP aliases are optional and conservative.

   Rationale: CLI aliases such as `exomem ask` and `exomem remember` improve human ergonomics without changing MCP schema snapshots. MCP clients already benefit from bootstrap action metadata, and existing canonical tools remain stable.

   Alternative considered: add many new MCP tools. Rejected for the first pass because more MCP tools may make tool selection noisier unless the aliases are extremely well scoped and separately schema-pinned.

3. `ask` should route to `find` with safe defaults and expose an explicit enriched mode.

   Rationale: Normal recall should be cheap and predictable. Rich graph/pack behavior is valuable, but it should be selected by an action option such as `--deep` rather than hidden behind every lookup.

4. `remember` should route to compiled notes; `capture` should route to raw sources/evidence.

   Rationale: This preserves the epistemic hierarchy in product language. Durable conclusions belong in `note`; raw material belongs in `add` or upload/preserve flows.

5. Bootstrap should emit a first-class action catalog.

   Rationale: Generic agents need a small map from intent to route. They do not need to learn every canonical tool before doing useful work.

## Risks / Trade-offs

- Alias semantics become vague -> Keep aliases small and map each one to a canonical operation plus explicit defaults.
- MCP tool count grows too large -> First pass can avoid MCP aliases and expose action guidance through bootstrap/scaffold docs.
- Users think simple actions are less capable -> CLI help and docs should point from aliases to canonical advanced tools.
- Hidden writes become risky -> Write aliases must require the same required fields and safety checks as the underlying canonical command.
- Graph/pack calls become slow by default -> Keep deep graph enrichment explicit and soft-failing; missing sidecars must return guidance rather than failing unrelated lookup.

## Migration Plan

1. Add action metadata and routing helpers over the existing command registry.
2. Add CLI aliases with human-readable help and JSON support where practical.
3. Update bootstrap/action catalog and scaffold guidance to teach actions first.
4. Regenerate capabilities docs and fixtures as needed.
5. Validate with focused CLI/bootstrap/schema tests before running the broader suite.

Rollback is simple: aliases and metadata can be removed without changing canonical commands or stored vault data.

## Open Questions

- Should MCP expose actual alias tools in this change, or should bootstrap guidance remain the first MCP simplification step?
- Should `remember` support only compiled notes in the first pass, or also a guided "raw plus compiled" mode?
- Should `maintain` be read-only by default (`audit`, `attention`) with an explicit `--fix` for `audit_fix`?
