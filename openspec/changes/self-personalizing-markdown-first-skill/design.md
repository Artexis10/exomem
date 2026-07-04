## Context

Per-vault behavior is already config-driven: `src/exomem/access.py` reads
`Knowledge Base/_access.yaml` (`readonly:`/`excluded:` subtree-prefix lists, hot-reloaded,
hard write-refusal) and `vault.CURATED_TREES` is empty — no engine code branches on
personal folder names. `overview()` already returns a one-call vault-structure scan (top
folders + counts + junk + KB presence). Yet the skill prose names personal folders and
assumes Obsidian, and the maintainer keeps a private GENERIC-marker canonical rendered
two ways (`genericize-schema.py`, `rebuild-schema-zip.py`). This change closes that gap.
Builds on the `exomem` rename (PR #114).

## Goals / Non-Goals

**Goals:**
- The shipped generic skill adapts to any vault at runtime with zero hand-editing.
- One command turns a vault scan into a starter `_access.yaml` — no hand-written YAML.
- Docs stop assuming Obsidian; the `_access.yaml` mechanism is user-visible.
- The public scaffold is the single source of the skill.

**Non-Goals:**
- `EXOMEM_KB_DIRNAME` (configurable governed-folder name) — separate change.
- An MCP write tool for personalize (v1 is CLI + `setup` only).
- Seeding `project-keys.yaml` (auto-registration already covers it).
- Re-documenting the "I already have a vault" narrative owned by the
  `document-existing-vault-onboarding` change.

## Decisions

- **Runtime discovery over baked personalization.** The skill instructs Claude to call
  `overview` on first engagement rather than shipping a per-user generated SKILL.md.
  Rationale: keeps one generic artifact; `overview` is one bounded call and already the
  read-side source of truth. *Alt considered:* codegen a personalized SKILL.md at install
  — rejected (per-user artifact to maintain; drifts from the vault).
- **`personalize` classifies on measured signals, not names.** Heuristic from `overview`
  output: empty → unmanaged; junk-dominant (≥50% sync-conflict/zero-byte) → `excluded`;
  markdown==0 & binary-heavy (≥90%) → `excluded`; else → `readonly`. Rationale: name
  denylists are speculative; measured signals match the engine's own model. Undercount is
  safe (falls through to `readonly` = findable + write-protected, never data loss).
- **Non-destructive YAML round-trip merge.** `merge_access_yaml` loads the existing file
  to a dict, appends de-duped sorted additions under a fixed header, re-emits
  (`sort_keys=False`) → byte-stable/idempotent, preserves existing + unknown keys. *Alt:*
  text-append (like `project_keys`) — rejected: inserting into the middle of existing list
  blocks is fiddly/bug-prone; `_access.yaml` is tool-owned so regenerating comments is fine.
- **CLI + `setup` step, no MCP tool.** Standalone `exomem personalize` (durable home,
  matches `init`/`install-skill`) plus an idempotent converger step in `run_setup` after
  `init`. Rationale: `excluded` hides content from search — a human-judgment write; an LLM
  auto-excluding could silently break recall. The skill instead *recommends the user run
  `exomem personalize`*. Slots into the MCP registry later against the same core if wanted.
- **Pure core + injectable seams.** `classify_siblings`/`merge_access_yaml` are pure
  (unit-tested on synthetic `overview` dicts); `scan_and_classify`→`write_access_yaml`
  split lets both callers render the proposal before writing without scanning twice;
  `input_fn`/`print_fn`/`overview_fn` seams mirror `setup_wizard` (no `home`/`run_fn` — one
  in-vault write).
- **Retire markers by inlining, not re-plumbing.** The scaffold already has no markers, so
  `rebuild-schema-zip.py` repoints to read the public scaffold verbatim and overlay the
  maintainer's real `project-keys.yaml`. `LEAK_PATTERNS` moves into its sole consumer
  (`tests/test_scaffold_no_leak.py`), dropping a fragile hyphenated-filename path-import —
  a net simplification.

## Risks / Trade-offs

- **Deleting `genericize-schema.py` breaks the no-leak test import** → relocate
  `LEAK_PATTERNS` verbatim into the test in the same commit; both structural scans stay
  byte-identical.
- **`personalize` mis-classifies a folder** → only ever proposes; interactive step shows
  the proposal and asks before writing; `readonly` (the default) is non-destructive and
  reversible by hand-editing; `excluded` is the only "hides content" call and requires the
  binary-heavy/junk-dominant signals.
- **`overview` breadth cap (~12 top folders)** → surface `children_omitted` as
  `report.cap_omitted` with a "re-run or add manually" note rather than silently dropping.
- **Maintainer's private marker canonical is abandoned** → out-of-repo, manual; his
  installed skill becomes the public scaffold via `install-skill`, his scopes live in his
  own `project-keys.yaml`/`_access.yaml`.

## Migration Plan

Prose (Part A) and tooling (Part C) are non-breaking. `personalize` (Part B) is additive;
`setup` gains an idempotent step. No data migration. Rollback = revert commits; no
persisted state beyond the user-owned `_access.yaml` it writes (which it only adds to).
