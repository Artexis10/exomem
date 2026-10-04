## ADDED Requirements

### Requirement: The MCP tool surface has a size budget

The generated MCP tool surface SHALL stay within a committed byte budget so that clients which resend every tool schema each turn do not pay for prose an agent does not need. The budget SHALL be measured as the compact UTF-8 JSON size of each registered tool's complete wire object (name, title, description, input schema, output schema, annotations and metadata) and SHALL apply to the total and to each tool. A tool description SHALL keep every refusal code, guard flag and destructive-operation requirement that the tool's callers depend on; long reference material SHALL be reachable from the skill references or an on-demand bootstrap profile rather than repeated in schemas. A contract that is shared by several tools SHALL be projected once per tool that needs it and SHALL NOT be repeated in parameter descriptions.

#### Scenario: The surface regrows past the budget

- **WHEN** a change adds description or parameter prose that takes the total, or any one tool, over its committed budget
- **THEN** the budget test fails and names the tool and the excess

#### Scenario: Shrinking preserves published behaviour

- **WHEN** the tool descriptions are shortened
- **THEN** no tool, parameter, enum value or refusal code is removed, and the tool-surface fingerprint and schema-fidelity baseline are regenerated together in one change

#### Scenario: Frozen hosted profiles are unaffected

- **WHEN** the live tool surface is shortened
- **THEN** hosted candidates v1 to v4 and the command-binding candidate resolve their pinned legacy schemas and stay byte-identical

#### Scenario: Optional parameters accept an explicit null the schema no longer advertises

- **WHEN** a client sends an explicit null for a nullable optional parameter of any tool
- **THEN** argument validation accepts it, because validation is built from the function signature and the published schema omits the null arm and the null default

### Requirement: Compact tools remain usable through the public interface

The compact surface SHALL preserve enough public guidance to choose an operation and construct a legal call without a provider-specific skill or private harness instruction. Current-turn activation and targeted retrieval SHALL have distinct descriptions consistent with saved engagement. Action-dependent Planning arguments SHALL explain the returned identity/version guards and the inspect/query-to-update/triage sequence. Runtime concurrency, authorization, source preservation and confirmation rules SHALL remain unchanged. This delivery SHALL retain existing tool names; shortening schemas SHALL NOT by itself establish a claim of improved agent performance.

#### Scenario: An unfamiliar agent updates a plan

- **WHEN** an agent uses the published tool/schema guidance to inspect a plan and update or transition it
- **THEN** it can identify and supply the returned guards, and a stale guard still refuses without overwriting newer state

#### Scenario: Interface and compiler failures are distinguished

- **WHEN** an ordinary-agent workflow selects activation or retrieval and receives a result
- **THEN** acceptance retains the actual invocation, result and subsequent answer, distinguishing wrong selection or arguments from wrong compiled context and never treating a forced call as proof of spontaneous initiation
