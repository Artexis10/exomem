# add-vault-overview — design

## Context

`audit` is KB-lint by contract; `doctor` is install preflight; `list_directory`
returns raw entry lists. None answers "what is the shape of this vault?" cheaply,
so agents fall back to reading every file. The guided-setup wizard (sibling change
`add-setup-wizard`) additionally needs to scan a vault *before* `init` runs — and
`vault.resolve_vault()` refuses roots without `Knowledge Base/_Schema/SKILL.md`,
so the scanning core cannot go through the usual vault resolution.

## Goals / Non-Goals

**Goals:**
- One bounded, deterministic, read-only structure report per vault subtree.
- Reachable from all three doors via the registry; callable pre-init as a plain
  function.
- Fast and dependency-free: one `os.walk`, `stat()` for non-markdown, capped
  content reads for markdown only.

**Non-Goals:**
- No lint/fix proposals (that is `audit`'s job, KB-scoped).
- No content search or ranking (that is `find`).
- No writes, ever — not even index refresh.
- No Obsidian-config parsing (`.obsidian/` is skipped, not interpreted).

## Decisions

1. **New Tier 1 registry op, not an `audit` mode or `doctor` check.**
   `audit`'s spec, docstring, and tests all define it as KB-only lint with
   proposals; overloading it muddies both contracts. `doctor` is CLI-only install
   preflight and not MCP-reachable — but MCP reachability is the point (agents are
   the ones bulk-reading). Tier 1 (not 2) so `EXOMEM_DISABLE_TIER2` cannot hide
   the very tool that prevents full-vault scans; precedent: `find scope="vault"`
   is Tier 1 and already walks the whole vault.

2. **Core/leaf split: `overview.overview(root, ...)` takes a raw `Path` and does
   not require an initialized KB.** The leaf `op_overview(vault_root, path="",
   ...)` resolves the subtree and delegates. The wizard imports the core directly.
   Alternative (leaf-only) rejected: it would force the wizard through
   `resolve_vault()`, which refuses un-init'ed roots.

3. **Own skip-set, deliberately different from `VAULT_SCAN_SKIP_DIRS`.**
   `{".obsidian", ".git", "_trash", "_attachments", "node_modules", ".trash"}`
   plus other dot-dirs unless `include_hidden=true`. Unlike the link-scan set,
   `_Schema` is NOT skipped — this is a structure report and `_Schema` is
   structure. The report lists what it skipped (`skipped.dirs`) so nothing is
   silently invisible.

4. **Bounded output is a requirement, not polish.** Depth cap (`max_depth`,
   default 3 — deeper folders roll up into their ancestor), breadth cap (top
   folders per level by recursive file count, with an explicit `omitted` count),
   per-file content-read cap (~512 KB; over-cap files counted in
   `skipped.oversized_files`), capped junk/sample/largest/oldest lists with exact
   totals alongside. Counts are always exact even where lists are capped.

5. **Content stats from markdown only.** Frontmatter presence = file starts with
   `---`; wikilink/md-link counts via regex on the capped read. Binaries are
   `stat()` only. Naming patterns derived by shape-bucketing filenames (digit runs
   → `N`, e.g. `2026-03-02 note.md` → `NNNN-NN-NN note.md`) and reporting the
   dominant buckets per folder — measurement, no interpretation.

6. **Sync-conflict detection is name-based:** `<base> <digits>.<ext>` where
   `<base>.<ext>` exists in the same folder, plus filenames containing
   `conflicted copy` / `sync-conflict` tokens. Zero-byte files reported
   separately. Proposals/fixes are out of scope (read-only op).

## Risks / Trade-offs

- [Huge vaults make even a walk slow] → single pass, no hashing, no content reads
  outside markdown-under-cap; breadth/depth caps bound the *output* while totals
  stay exact.
- [Schema fidelity baseline drift] → regenerating
  `tests/fixtures/mcp_tool_schemas.json` is an explicit late task; docstring is
  final before regeneration.
- [Scaffold leak via SKILL.md examples] → generic folder names only
  (`Daily/`, `Journal/`); `test_scaffold_no_leak.py` gates.
- [Windows path separators in report paths] → all reported paths POSIX-normalized
  (`as_posix()`), matching existing ops.

## Open Questions

(none — parameters and shape settled above)
