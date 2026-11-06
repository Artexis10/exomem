# Tasks

## 1. Require the hash

- [x] 1.1 Make `expected_hash` required on the `replace_body` variant and refuse a whole-body edit without it in `op_edit`, with one rule text.
- [x] 1.2 Update the edit documentation, the scaffold operations reference and the generated plugin tree.
- [x] 1.3 Regenerate the MCP schema baseline, tool-surface digest and Cloud plugin packages, and record the digest as pending in the ChatGPT rollout contract.
- [x] 1.4 Add tests for the nested and the flat refusal; give existing whole-body test edits their page hash.

## 2. Delivery

- [x] 2.1 Review the exact change independently; resolve findings.
- [x] 2.2 Merge, then synchronize this delta into the canonical specs and archive this change with `openspec archive`.
