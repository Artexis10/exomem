# Vault consolidation — gated migration runbook

**Status: STAGED. Nothing here runs automatically.** The code that makes this
safe (whole-KB extraction, dataset cards, `find` file-type filters, the
`access.py` tier system + central write-guard) is merged and tested. This
runbook is the **irreversible** other half: physically folding the vault's
sibling folders into `Knowledge Base/` so the KB becomes the single coverage
boundary. Run it deliberately, with a backup, on the primary host (the laptop
server deploys/syncs separately — do it there afterward or let sync carry it).

> Decision recap (yours): **everything** under `Knowledge Base/`; curated-thinking
> folders come in but are marked **`readonly`** via `_access.yaml`; keep an
> archived copy of the old structure marked deprecated. Live data is fine to move
> in — append-only only governs `Sources/`/`Evidence/`, not the whole KB.

Throughout, `"$VAULT"` = your vault root (the folder that contains
`Knowledge Base/`). Confirm it before starting:
`echo $env:KB_MCP_VAULT_PATH` (PowerShell).

---

## Step 0 — Pre-flight (read-only; do this first)

1. **Inventory the siblings.** List the top-level vault folders so you decide
   exactly what moves in:
   `Get-ChildItem -Directory "$VAULT" | Select-Object Name`
   - **Move in:** data folders (`Finance`, `Tracking`, …) and curated-thinking
     folders (`Cognitive Core`, `Domains`, `Prompt Bank`, `Products`,
     `Personal Context (Evolving)`, `Systems Thinking`).
   - **Leave alone:** `Knowledge Base` itself, `.git`, `.obsidian`, any
     `*-backup-*`.

2. **Find hardcoded references to those folders** before moving them — other
   skills/tools may reference e.g. `Cognitive Core/...` as a *sibling* path that
   becomes `Knowledge Base/Cognitive Core/...` after the move:
   `Select-String -Path "$VAULT\**\*.md" -Pattern '\[\[(Cognitive Core|Domains|Prompt Bank|Products|Personal Context|Systems Thinking|Finance|Tracking)/' -List`
   Wikilinks usually still resolve (the resolver is KB-prefix-tolerant), but note
   any **tool/skill configs or code** outside the vault that hardcode these paths
   (e.g. the cv-build skill) and update them after.

---

## Step 1 — Backup + deprecation marker (the archive you asked for)

1. **Full timestamped copy** (non-destructive; copies to a fresh dir):
   `robocopy "$VAULT" "$VAULT-backup-20260624" /E /R:1 /W:1 /XD "$VAULT\.git"`
2. Keep that backup until you've verified the migrated vault for a few days. It
   *is* the rollback (Step 7).
3. After the move (Step 3), drop a deprecation note at the vault root recording
   the old layout — Claude writes this during the supervised run, or:
   create `"$VAULT\Knowledge Base\DEPRECATED-STRUCTURE.md"` describing the
   pre-migration sibling layout and the migration date.

---

## Step 2 — SKILL update (vault canonical) + re-derive

The canonical SKILL is the **vault** `_Schema/SKILL.md`, not the repo scaffold
(never hand-edit the scaffold). Apply these additions there, **bump `version:`**,
then re-derive both surfaces.

**Edits to `Knowledge Base/_Schema/SKILL.md`:**

- **Searchable binaries are vault-wide now.** Note that *any* binary placed
  anywhere under `Knowledge Base/` (not only `Evidence/`) is OCR/ASR/PDF-extracted
  into a `.md` companion and becomes findable — so invoices, receipts, and
  screenshots filed under a data folder are searchable by content.
- **Tabular data → dataset card + `query_data`.** Document the flow: call
  `query_data(path=<csv/json>, aggregate="profile")` to get a content profile
  (vendors, item names, totals, date span) **and** a ready `dataset` card; write
  the card into the KB (fill its "What this holds" line); retrieve exact rows via
  `query_data`. **Raw rows are never embedded** — search the card, retrieve the
  rows. Salient single records get promoted to notes/entities as usual.
- **`find` file-type filters.** `file_types` / `exclude_file_types` over
  `note/pdf/image/audio/video/csv/json`. Default returns **all** kinds — search
  never hides a type unless asked. Mention `aggregate="profile"` on `query_data`.
- **Access tiers (`_access.yaml`).** The KB is now the whole vault; per-subtree
  access is governed by `Knowledge Base/_access.yaml`: `readonly` (findable,
  never written — the curated-thinking trees), `excluded` (private: not indexed,
  not written), with `Sources/`/`Evidence/` append-only as before. Reads are
  unrestricted. Note that curated trees (`Cognitive Core/`, `Domains/`,
  `Prompt Bank/`, `Products/`, `Personal Context/`, `Systems Thinking/`) are
  read-only inputs even though they now live inside the KB.
- **`prefer_compiled=false` for record hunts.** When hunting a specific line
  item / asset / transaction, pass `prefer_compiled=false` so raw captures aren't
  down-weighted under compiled notes.

**Then re-derive (repo CLAUDE.md rule):**
- `python scripts/genericize-schema.py --vault "$VAULT"`  (regenerates the repo scaffold; commit it)
- `python scripts/rebuild-schema-zip.py --vault "$VAULT"`  (rebuilds `_Schema.zip`; re-upload to claude.ai)
- Reminder: copy the gitignored `scripts/generic/{substitutions,leakguard}.txt`
  from the primary checkout first if re-deriving in a fresh worktree (see the
  "genericize worktree leak" note).

---

## Step 3 — Move the siblings under `Knowledge Base/` (IRREVERSIBLE)

Stop the service first so nothing reads mid-move:
`scripts/restart.ps1 -Stop`  (or stop the NSSM service).

Then, **one folder per line** (repeat per folder you chose in Step 0):
`Move-Item "$VAULT\Finance" "$VAULT\Knowledge Base\Finance"`
`Move-Item "$VAULT\Tracking" "$VAULT\Knowledge Base\Tracking"`
`Move-Item "$VAULT\Cognitive Core" "$VAULT\Knowledge Base\Cognitive Core"`
`Move-Item "$VAULT\Domains" "$VAULT\Knowledge Base\Domains"`
`Move-Item "$VAULT\Prompt Bank" "$VAULT\Knowledge Base\Prompt Bank"`
`Move-Item "$VAULT\Products" "$VAULT\Knowledge Base\Products"`
`Move-Item "$VAULT\Personal Context (Evolving)" "$VAULT\Knowledge Base\Personal Context (Evolving)"`
`Move-Item "$VAULT\Systems Thinking" "$VAULT\Knowledge Base\Systems Thinking"`

Wikilinks stay valid (the resolver matches both `[[Cognitive Core/X]]` and the
new `[[Knowledge Base/Cognitive Core/X]]`). No bulk rewrite needed.

---

## Step 4 — Seed `_access.yaml` (the off-limits policy)

Create `Knowledge Base/_access.yaml` marking the curated-thinking trees
`readonly` (Claude writes this during the supervised run). Content:

```yaml
# Per-subtree access tiers. Folder paths are KB-relative; each entry covers the
# whole subtree. readonly = findable but never written; excluded = private
# (not indexed, not written). Sources/ and Evidence/ stay append-only (built-in).
readonly:
  - Cognitive Core
  - Domains
  - Prompt Bank
  - Products
  - Personal Context (Evolving)
  - Systems Thinking
excluded: []
```

Live-loaded — no restart needed for policy changes. Add folders to `excluded`
later if you want any truly private (hidden from `find`).

---

## Step 5 — Re-index everything

Start the service: `scripts/restart.ps1`. Then, from the repo (GPU host):
- Rebuild embeddings over the now-larger KB:
  `uv run kb-mcp audit-fix --rebuild-embeddings`  (or the `rebuild_all` entrypoint)
- Back-fill media companions for any binaries newly inside the KB
  (invoices/receipts/screenshots under the moved data folders):
  `uv run kb-mcp backfill-media`
- For each CSV/JSON you want findable, create its dataset card (Step 2 flow).

---

## Step 6 — Verify (evidence before declaring done)

- **Invoice is searchable:** in claude.ai, `find "Ugreen Nexode 100W charger"`
  and `find "USB4 240W cable"` — the invoice surfaces with **no** hand-typed
  register entry needed.
- **Curated docs are findable but locked:** `find` a Cognitive Core topic →
  it appears; then have the skill attempt an `edit`/`create_file` there → refused
  (`WRITE_REFUSED … readonly`).
- **Tabular by content:** `find "Mavic battery"` hits the dataset card;
  `query_data` returns the exact rows.
- **Connector still healthy:** a fast `401` at the funnel (per the triage note);
  don't restart reflexively.

---

## Step 7 — Rollback (if anything's wrong)

The migration is just a folder move + a config file + an embedding rebuild. To
revert: stop the service, delete the migrated `Knowledge Base/<Folder>` copies,
restore the sibling folders from `"$VAULT-backup-20260624"`, remove
`_access.yaml`, restart, rebuild embeddings. The backup is the source of truth.
