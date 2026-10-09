## Why

`exomem install-hook` writes only the deployed hook config. Where that config is managed by yadm's alternate mechanism, the deployed file is regenerated from an `##`-suffixed source, and `yadm alt` re-runs alternate selection after ordinary commands such as `yadm status`. An install that edits only the deployed file is therefore undone with no visible trigger — the same defect `uninstall_hook` was given a route around in #656 (and #580 before it), where a prune spans the deployed file and every alternate source.

The two halves never became symmetric. `_alternate_sources()` has exactly one call site in the module, inside `uninstall_hook`. Exomem can remove itself from a yadm-managed machine but cannot durably install itself on one.

Where yadm links rather than copies, install does not merely get reverted — it cannot run at all. The deployed name is a symlink to its source (`settings.json -> settings.json##os.WSL`), and the config read guard requires a regular file, so every read fails with `unsafe hook config file` before any merge is attempted.

A second defect compounds it. When that read fails, `install-hook --check` still reports `PASS config.legacy: no legacy kb_* hook entries configured` — on a config holding four legacy entries. The existing requirement is that `--check` SHALL validate legacy entries; when the config cannot be read the legacy check never runs, yet it is scored as a pass. An operator auditing a machine is told it is clean by a check that did not execute, which is worse than being told nothing.

## What Changes

- Teach the install merge path the same alternate-source awareness the uninstall prune path already has: a merge SHALL reach every parseable alternate source for the target config, so the entries survive alternate regeneration.
- Accept a deployed config that is a yadm alternate link under a narrow, explicitly bounded relaxation of the regular-file guard: the link target must be a same-directory sibling whose name is the deployed name plus the `##` alternate suffix, and the target itself must satisfy every existing ownership and writability check. Any other symlink is still refused.
- Report a check that could not be evaluated as its own state, never as a pass. When the config read fails, every content-derived condition SHALL be reported as unevaluated and the run SHALL fail.
- Preserve current behavior exactly where no alternate source exists, so a plain single-file install is unchanged.

## Capabilities

### Modified Capabilities

- `compaction-continuation-checkpoints`: extends the multi-client installation contract to alternate-managed configs, and strengthens the diagnostics contract so an unevaluated condition can never be reported as a pass.

## Impact

This affects the `install-hook` merge and check paths and their fixtures. It changes no hook runtime behavior, no capture or retrieval semantics, no MCP schema, and no Markdown source-of-truth rule. The regular-file guard is relaxed only for a link whose target is a same-directory alternate sibling; every ownership, group/other-writability, size, and atomic-replacement guarantee is retained unchanged.
