## Why

The shipped skill reads as **someone else's vault**: its prose names specific personal
folders and assumes an Obsidian layout, even though the engine never branches on those
names — per-vault behavior is decided only by `Knowledge Base/_access.yaml`
(`readonly:`/`excluded:`), the "outside `Knowledge Base/` is read-only" rule, and
auto-registered `project-keys.yaml`. So new users can't tell how to make it theirs, and
the maintainer hand-maintains a private GENERIC-marker canonical to render two skills
from one file. This change makes the generic skill self-adapt to any vault at runtime,
gives users a one-command way to generate their access policy, drops the Obsidian
assumption, and retires the marker canonical so the public scaffold is the single source.

## What Changes

- **Self-discovering skill (prose):** the shipped `_scaffold/_Schema/SKILL.md` +
  `references/*.md` stop assuming a fixed vault shape and instead instruct Claude to run
  the existing `overview` tool on first engagement to learn a vault's real top-level
  layout, treating everything outside `Knowledge Base/` as read-only input.
- **Markdown-first:** reword "Obsidian vault" → "markdown vault (Obsidian optional)";
  keep wikilinks + YAML frontmatter stated as required, mark Dataview / callouts /
  Obsidian Sync as optional niceties.
- **Surface `_access.yaml`:** document the existing (code-only) `readonly:`/`excluded:`
  access config in `references/write-scope.md` and reference it from the skill.
- **New `personalize` command:** `exomem personalize` (+ a step in `exomem setup`) scans
  a vault via `overview`, classifies top-level sibling folders by measured signals, and
  **non-destructively** writes/merges `<vault>/Knowledge Base/_access.yaml`. Default-off
  of nothing heavy (no model, no network); soft-fails (missing `Knowledge Base/` →
  structured `PersonalizeError`; re-runnable converger; never removes user entries). No
  MCP write tool in v1 (choosing `excluded` is human judgment; `overview` already gives
  Claude the read side).
- **Retire the GENERIC-marker canonical:** delete `scripts/genericize-schema.py`
  (relocating its `LEAK_PATTERNS` into `tests/test_scaffold_no_leak.py`), delete
  `tests/test_schema_markers.py`, and repoint `scripts/rebuild-schema-zip.py` to build the
  maintainer's claude.ai `.skill` from the public scaffold + overlaid `project-keys.yaml`.

## Capabilities

### New Capabilities
- `vault-personalization`: scan a vault, classify top-level sibling folders
  (readonly/excluded/unmanaged) by measured signals, and generate/merge
  `Knowledge Base/_access.yaml`; plus the runtime self-discovery contract for the skill
  (run `overview` first; outside-KB is read-only; `_access.yaml` overrides).

### Modified Capabilities
- `install-readiness`: the `setup` wizard gains an idempotent personalize step after
  `init`, so guided onboarding produces a starter access policy for existing sibling
  folders.

## Impact

- **Code:** new `src/exomem/personalize.py`; `src/exomem/__main__.py` (dispatch +
  docstring), `src/exomem/setup_wizard.py` (step). Reuses `overview.overview`,
  `access._load_config`/`access_tier`, and the `setup_wizard` converger patterns. No
  engine/behavior change to `find`/writers — `_access.yaml` is already enforced.
- **Skill scaffold (docs):** `_scaffold/_Schema/SKILL.md`, `references/*.md`, `index.md`
  — prose only, must stay generic (leak-guarded).
- **Maintainer tooling (chore, no capability delta):** remove `genericize-schema.py` +
  `test_schema_markers.py`; repoint `rebuild-schema-zip.py` (+ `.ps1`/`.sh`); update
  `CLAUDE.md`, `CONTRIBUTING.md`, `.gitignore`. `LEAK_PATTERNS` moves into
  `tests/test_scaffold_no_leak.py` (its only live consumer).
- **Command surface:** `personalize` is reachable via CLI + `setup` only; deliberately
  not on the MCP/REST registry in v1.
- **Builds on** PR #114 (skill renamed `knowledge-base` → `exomem`). **Out of scope:**
  `EXOMEM_KB_DIRNAME` (configurable governed-folder name) is a separate future change.
