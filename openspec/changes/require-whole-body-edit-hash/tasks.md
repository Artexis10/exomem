# Tasks

## 1. Require the hash

- [ ] 1.1 Make `expected_hash` required on the `replace_body` variant and refuse a whole-body edit without it in `op_edit`, with one rule text.
- [ ] 1.2 Update the edit documentation, the scaffold operations reference and the generated plugin tree.
- [ ] 1.3 Regenerate the MCP schema baseline and tool-surface digest, and record the digest as pending in the ChatGPT rollout contract.
- [ ] 1.4 Add tests for the nested and the flat refusal; give existing whole-body test edits their page hash.

## 2. Delivery

- [ ] 2.1 Review the exact change independently; resolve findings.
- [ ] 2.2 Merge, then synchronize this delta into the canonical specs and archive this change with `openspec archive`.
