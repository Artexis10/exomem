# Audit checks

Per-check detail for the **audit** (lint) operation — what each check flags, its
severity behaviour, and the proposed fix. SKILL.md § "Audit (lint) checks" lists
the check names; read this when running or acting on an audit. Audit is
read-mostly: the output is a proposal report you review; nothing is rewritten
without explicit confirmation per item or batch.

The audit call itself is always read-only. Its default `detail="actionable"`
orders current blockers first, malformed/unregistered relation work next, and
ordinary findings after that. Grandfathered relation-disposition findings are
grouped into one deterministic `legacy_backlog` carrying exact observed,
available, omitted, and upstream-truncation counts plus bounded samples. Use
`detail="full"` for raw finding enumeration. `legacy_sample_limit` accepts only
integers from 0 through 50 and changes actionable sampling, never the checks or
stored knowledge.

- **Orphans** — compiled pages with zero inbound links and zero outbound links beyond their `sources` block. Propose: link or archive.
- **Broken wikilinks** — `[[X]]` where `X` does not resolve. The audit skips wikilinks inside fenced code blocks and inline code spans. Bare names resolve against filename stems AND frontmatter `title:` (so date-prefixed sources with a title match are not flagged); a link carrying an explicit non-`.md` extension (`[[…/scan.pdf]]`) resolves if that file exists on disk, matching Obsidian's attachment links. Findings inside append-only trees (`Sources/`, `Evidence/`) — which can't be repaired in place — are surfaced at `info` severity, keeping them out of the actionable `warn` set. Propose: fix path or create stub entity.
- **Supersession integrity** — pages marked `superseded` must have `superseded_by` pointing to a real page; pages marked `active` must not appear as the target of any `superseded_by`.
- **Stale frontmatter** — required fields missing for the page type. Includes: patterns with `project:` (singular) when `projects:` (plural) is the convention for cross-project patterns.
- **`index.md` / `log.md` drift** — files in folders that are not catalogued, catalogue entries pointing to missing files, or `log.md` entries without corresponding artifacts on disk (and vice versa).
- **Unprocessed sources** — `Sources/` files with empty `ingested_into:`, **aged and triaged oldest-first**: each finding carries `meta` (`age_days`, `age_bucket` ∈ fresh <30d / aging <90d / stale, `captured`) and escalates to `warn` once stale. Drain the worst rot first: pick a coherent set of the oldest, call **`propose_compilation(sources=[…])`** for a ready-to-fill scaffold, then compile via **note**.
- **Status / location mismatch** — pages with `status: archived` not living in an `_archive/` subfolder, and vice versa.
- **Unfinished experiments** — experiments with `status: active` and `started` date older than the experiment's `duration` field. Propose: write up results, mark concluded, or extend.
- **Unfinished production lifecycles** — production-logs with `status: recorded` or earlier whose `published` field has been null for >60 days. Propose: update status, fill outcomes, or move to dropped.
- **Stale hubs / snapshots** — a *separate* concern from `stale_review` (which deliberately **excludes** hubs/snapshots): research-notes that play the hub or snapshot role are **expected** to drift — a hub refreshes on a major capability ship, a snapshot is point-in-time by design — so they're not ordinary stale-review candidates. No automated check currently flags these on their own cadence; refresh a hub when a major ship warrants it and treat snapshots as historical.
- **Unregistered project key** — pages with a `project:` or `projects:` value not in `_Schema/project-keys.yaml`. Catches drift from Tier 2 `create_file` escape-hatch writes that bypass the auto-register flow. Propose: fix the value via `edit` (frontmatter-patch mode; its typo guard will surface the intended key) or hand-add the new key to the YAML.
- **Graph drift** (`graph_drift`) — derived `.graph.sqlite` state missing, stale, disabled, or schema-incompatible. Propose: run `reconcile` to rebuild or refresh the sidecar. When graph indexing is disabled, drift checks are a read-only no-op and report availability metadata instead of loading optional dependencies.
- **Embedding drift** — sidecar rows whose row mtime is older than the on-disk file mtime (likely an out-of-band editor edit, e.g. Obsidian, that bypassed exomem's writer hooks). Propose: run `reconcile` (incremental, stale rows only) or `audit_fix(rebuild_embeddings=true)` (full wipe + rebuild) to refresh.
- **Stale review** (`stale_review`) — active compiled *conclusions* (research-note / insight / pattern / failure / entity) that are simultaneously **old** (frontmatter `updated`/`created` beyond `EXOMEM_STALE_AGE_DAYS`, default 365), **rarely surfaced** by `find` (appearances in `logs/queries.jsonl` `top_k` ≤ `EXOMEM_STALE_MAX_ACCESS`, default 1), and **low inbound-link degree** (wikilink-graph in-degree ≤ `EXOMEM_STALE_MAX_INBOUND`, default 1). A measurement-only review queue at `info` severity: it **surfaces** candidates (with `meta`: `age_days`, `age_bucket`, `inbound_count`, `access_count`) for you to judge, and **never** decays, down-ranks, hides, or moves anything — `find` ordering is unchanged (surfacing a candidate ≠ a forgetting curve). All three signals derive from what the KB already records (frontmatter dates, the link graph, the query log) — no new sidecar. AND-gated as a filter, not a score (no confidence concept). The access signal is gated by `EXOMEM_DISABLE_RELEVANCE_CHECK`; when the log is unavailable that conjunct is **dropped** (absence is "unknown", never a fabricated zero-access), so the gate falls back to age AND low-inbound. Scope is governed by access tier (read-write) + a conclusion type, so it spans the whole writeable KB (not a fixed folder list) and auto-excludes readonly trees, append-only `Sources/`/`Evidence/`, superseded/archived, index files, and hubs/snapshots. Propose: confirm still true (keep), `replace` (supersede), or archive.
- **Relation debt** (`relation_debt`) — active writable compiled pages with no
  outbound body wikilinks or canonical note/block relations. It excludes
  archived/superseded pages, indexes, append-only/read-only trees, hubs, and
  snapshots. This is an informational Inbox proposal, not a write failure:
  inspect `connect_memory(operation="suggest-relations")` or `suggest-links`,
  then add only defensible edges under `## Relations` (or block `relations:`
  metadata). Never turn semantic proximity into a durable edge automatically.
