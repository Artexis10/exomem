# Maintainer notes

## The skill scaffold is generated — don't hand-edit it

`src/kb_mcp/_scaffold/_Schema/` (the skill shipped to friends via `init` /
`install-skill`) is a **derived, genericized copy** of the canonical skill that
lives in the maintainer's Obsidian vault at `<vault>/Knowledge Base/_Schema/`. It
is produced by `scripts/genericize-schema.py` — the sibling of
`scripts/rebuild-schema-zip.*`, which derives the claude.ai `.skill` zip from the
same canonical. One canonical, derive the rest.

**Never edit `_scaffold/_Schema/` by hand.** Your change will be overwritten on
the next regeneration, and hand-editing is how personal data once leaked into the
shipped scaffold (a friend's clone contained the maintainer's machine paths and
sensitive `Evidence/` examples).

### To change the skill
1. Edit the canonical in the vault (`<vault>/Knowledge Base/_Schema/`).
2. **Bump `version:` in SKILL.md frontmatter** (semver). A content change without a
   version bump is a bug — do it in the same change.
3. Regenerate the repo scaffold: `python scripts/genericize-schema.py --vault <vault-root>`
   (or set `KB_MCP_VAULT_PATH`; `--check` dry-runs the guard). Commit the regenerated
   `_scaffold/_Schema/`.
4. Rebuild the claude.ai zip: `python scripts/rebuild-schema-zip.py --vault <vault-root>`,
   then re-upload `<vault>/Knowledge Base/_Schema.zip` to claude.ai.

### How genericization works
- **Block markers** in the canonical — `<!-- GENERIC-START … GENERIC-REPLACE --> … <!-- GENERIC-END -->`
  (invisible in rendered Obsidian) for multi-line personal regions; the text
  between START and REPLACE is what ships, the text after it is kept in the
  canonical but dropped from the output. Empty replacement = omit the block.
- **Substitutions** — `scripts/generic/substitutions.txt` (gitignored, `real => generic`
  per line) for scattered personal tokens (e.g. a real tenant key).
- `project-keys.yaml` is replaced wholesale by `scripts/generic/project-keys.yaml`.

### Leak-guard
The script aborts before writing if personal data would ship: a committed
**pattern guard** (machine-path shapes etc., `LEAK_PATTERNS` in
`genericize-schema.py`) plus an optional local denylist
(`scripts/generic/leakguard.txt`, gitignored) and the left side of every
substitution. `tests/test_scaffold_no_leak.py` runs the pattern guard against the
committed scaffold (no vault needed), so the leak class can't regress in CI.
