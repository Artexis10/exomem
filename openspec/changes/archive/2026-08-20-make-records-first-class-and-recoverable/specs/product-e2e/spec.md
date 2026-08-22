## MODIFIED Requirements

### Requirement: Installed-wheel stdio product loop

The system SHALL provide a black-box test that builds and installs the wheel, initializes a temporary vault through the installed CLI, connects through a real stdio MCP client, and completes source capture, source-backed memory, recall, read, graph context, evidence preservation, supersession, evolution review, reconcile, Records authoring/recovery, restart, and persistence checks. The Records portion SHALL begin without a pre-written collection manifest and SHALL exercise `describe`, create-mode `validate`, `create` with a no-filter saved view, `inspect`, `append`, `query`, `update`, revision-mode `validate`, `revise`, a direct-edit audit gap, and `rebaseline` through the installed command surface.

#### Scenario: Governed lifecycle survives restart
- **WHEN** the lean product E2E completes the governed lifecycle and restarts the stdio server
- **THEN** the active conclusion, preserved source/evidence links, supersession history, stable references, Record collection, revised saved view, appended/updated items, and persistent `acknowledged_gap` Record discontinuity remain resolvable

#### Scenario: Human-owned Records lifecycle survives restart
- **WHEN** the lean product E2E manually inserts a template block, performs guarded Records append/update calls, edits the canonical log while stopped, and restarts the stdio server
- **THEN** the installed `record_memory` surface returns the manual and guarded state, observes the later edit, reports its audit gap, preserves the Planning descriptor, and does not expose raw rows through ordinary recall

#### Scenario: Records fixture cannot bypass authoring
- **WHEN** the installed-wheel Records lifecycle starts in its temporary vault
- **THEN** no test helper writes `_collection.md` or its canonical source before `record_memory(action="create")` commits them

#### Scenario: Validate and inspect acceptance stay identical
- **WHEN** the E2E validates and creates a manifest whose saved view omits optional filters
- **THEN** immediate inspect and saved-view query succeed without manifest repair
