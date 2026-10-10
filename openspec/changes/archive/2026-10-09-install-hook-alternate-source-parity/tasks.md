## 1. Alternate-source merge on install

- [x] 1.1 Add a red test proving an install against a deployed config with a `##`-suffixed sibling leaves the sibling without the entries, then make it green by merging every parseable source.
- [x] 1.2 Add a red test proving a deployed config that is a link to a same-directory `##` sibling fails to install today, then make it green through the bounded link relaxation.
- [x] 1.3 Add refusal tests for every rejected link shape: absolute target, parent-directory target, non-`##` target name, and a link to a link. Each must write nothing.
- [x] 1.4 Add a test proving an unparseable alternate source is reported as skipped while the deployed config and parseable siblings are still merged.
- [x] 1.5 Add a test proving a no-alternate install is byte-identical to today, including backup and atomic-replacement behavior.
- [x] 1.6 Add a test proving a normalized no-op on a source creates neither backup nor rewrite.
- [x] 1.7 Derive the backup-name exclusion from the generator that mints it, and pin the two to each other with a round-trip test rather than a restated pattern.

## 2. Unevaluated conditions in `--check`

- [x] 2.1 Add a red test proving `--check` reports `PASS config.legacy` for an unreadable config that holds legacy entries, then make it green by reporting the condition as unevaluated.
- [x] 2.2 Make `overall` fail when any condition is unevaluated, and prove a readable config's report is unchanged.
- [x] 2.3 Confirm the human renderer never prints an unevaluated condition with pass vocabulary.

## 3. Truthful uninstall on an alternate-link deployment

- [x] 3.1 Add a red test proving `--uninstall` on a linked deployment reports the regular-file error and zero removals while the sibling lane removes the entries, then make it green by reporting the removal it performed.
- [x] 3.2 Add a red test proving the resolved target is rewritten in the sibling lane, then make it green so that visit is a no-op and exactly one backup exists on disk.

## 4. Verification

- [x] 4.1 Run the scoped suites: `tests/test_install_hook.py` and `tests/test_install_hook_uninstall.py`, named as the scope.
- [x] 4.2 Exercise a real yadm-shaped fixture end to end — copy deployment and link deployment — proving install then regeneration keeps the entries.
- [x] 4.3 Run the full suite at the completion boundary before opening the PR.

## Closure evidence (T10 audit, 2026-10-09)

- 4.3: PR #1474 merged as 2f9032fc4 and shipped in v0.100.0. CI on its head ran the full corpus (12 core shards and 4 harness shards), all green.
