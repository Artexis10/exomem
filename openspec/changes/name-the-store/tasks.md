## 1. Implementation

- [x] 1.1 Add `store_identity()` and `hosted_mode_enabled()` to `src/exomem/cloud_cell.py`; `resource_policy` calls the shared hosted parse.
- [x] 1.2 Append the store name to `user_summary` where `project_terminal` assigns it; change the replace sentence to ", was old".
- [x] 1.3 Carry `store` in the session profile and the compact reference (`diagnostics_reading` section).
- [x] 1.4 Add the store rule to the core skill and every standalone workflow skill; refresh the skill-contract stamps; regenerate the plugin trees.

## 2. Proof

- [x] 2.1 `store_identity()` names the three deployment modes.
- [x] 2.2 The bootstrap session profile and `section=all` carry `store.kind` `personal` by default and `cloud` with `EXOMEM_CLOUD_CELL=1`.
- [x] 2.3 `user_summary` names the store in personal and cloud mode, and a replay gives the same sentence.
- [x] 2.4 Every skill source carries the store rule.

## 3. Closure

- [x] 3.1 `openspec validate --all --strict` passes before and after the archive.
