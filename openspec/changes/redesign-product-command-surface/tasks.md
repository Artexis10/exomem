## 1. Product Command Registry

- [x] 1.1 Add product command metadata and route coverage over the existing canonical leaves.
- [x] 1.2 Define the default public product tool set and exact names.
- [x] 1.3 Add validation that every product route references an existing canonical leaf or explicit hand-registered helper.
- [x] 1.4 Add coverage validation that every non-terminal-local canonical capability has a product command route.

## 2. Product Command Implementations

- [x] 2.1 Implement read-only product commands for recall, page reading, browsing, review, graph connection, media reading, artifact transfer, and dataset querying.
- [x] 2.2 Implement additive/write product commands for remembering, source capture, evidence preservation, and source compilation.
- [x] 2.3 Implement governed mutation product commands for editing, replacing, moving, deleting, recovering, adopting, and maintaining.
- [x] 2.4 Ensure every product command routes through canonical leaves and preserves existing validation, write guards, and binary guards.
- [x] 2.5 Keep heavy/model-backed measurement paths explicit, default-off or mode-gated, and soft-failing.

## 3. Surface Generation

- [x] 3.1 Generate default MCP tools from the product command registry instead of the canonical primitive registry.
- [x] 3.2 Generate REST routes and OpenAPI paths from the same product command registry.
- [x] 3.3 Generate CLI subcommands from the same product command registry.
- [x] 3.4 Preserve terminal-local setup/admin commands outside the product registry where appropriate.
- [x] 3.5 Update result/error envelope handling so product REST and CLI paths share the same behavior.

## 4. Bootstrap, Scaffold, And Docs

- [x] 4.1 Update bootstrap to expose `product_commands` and product command defaults.
- [x] 4.2 Update scaffold `SKILL.md` and operation references to teach product commands as the public interface.
- [x] 4.3 Update `QUICKSTART.md`, assistant guide, capabilities docs, and knowledge-pack docs.
- [x] 4.4 Regenerate docs/capabilities or related generated artifacts if command metadata changes.

## 5. Tests And Fixtures

- [x] 5.1 Update MCP schema fixtures to the intentional product tool baseline.
- [x] 5.2 Add tests for exact default MCP product tool names and absence of primitive names.
- [x] 5.3 Add MCP/REST/CLI parity tests for representative read, write, review, maintenance, file, evidence, media, and dataset product commands.
- [x] 5.4 Add route coverage tests for product commands over canonical capabilities.
- [x] 5.5 Update bootstrap, tool annotation, REST registry, CLI core ops, scaffold leak, and product-flow benchmark tests.

## 6. Verification

- [x] 6.1 Run `openspec validate redesign-product-command-surface`.
- [x] 6.2 Run ruff on touched Python files.
- [x] 6.3 Run focused product-surface tests.
- [x] 6.4 Run generated-doc checks.
- [x] 6.5 Run full pytest suite.
- [x] 6.6 Run `git diff --check`.
