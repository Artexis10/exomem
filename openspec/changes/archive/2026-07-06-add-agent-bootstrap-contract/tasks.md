## 1. Bootstrap Contract

- [x] 1.1 Add a deterministic `op_bootstrap` leaf returning compact/full/diagnostics contract payloads.
- [x] 1.2 Register `bootstrap` in the command registry for MCP, REST, CLI, and OpenAPI with read-only annotations.
- [x] 1.3 Add unit and surface tests for bootstrap payload shape, profile validation, and generated MCP accounting.

## 2. Upload And Timing Metadata

- [x] 2.1 Extend preserve streaming results with SHA-256 hash, byte size, media identifier, and content type metadata.
- [x] 2.2 Return the new metadata from `/upload` while preserving existing success and error fields.
- [x] 2.3 Add timing profile metadata to `find(include_timings=true)` without changing untimed response shapes.

## 3. Documentation And Verification

- [x] 3.1 Update README/Quickstart generic-client guidance to call `bootstrap()` first when no native Skill is available.
- [x] 3.2 Update the committed MCP schema fixture for the intentional `bootstrap` tool addition.
- [x] 3.3 Run targeted tests for bootstrap, MCP schema fidelity, upload metadata, and find timings.
