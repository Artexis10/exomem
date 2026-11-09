## Context

Search builds its page rows in `lexstore.page_rows` and its embedding chunks in `embeddings._chunks_for_page`, both from a page's `search_body`. A preserved dataset contributes only its card, `<file>.json.md`, whose body holds structural metadata and no values (`source_inspection._inspect_dataset`). Search results pass through `governance/egress.annotate_hits`, which decides each hit by its path. A Markdown hit is judged on its own frontmatter. A non-Markdown path such as a dataset original takes the path-only decision `membership.evaluate_path_only`. Under a still-undecided semantic scope, that decision is unresolved for a new dataset, because its card carries no governance descriptor. `query_dataset` authorizes raw reads with the same path-only decision and requires the full release level.

So the card and the rows can have different read rules. A value copied into the card would be released under the card's rule. That is why the earlier change kept values out of cards, and the reason still holds.

Roster row consulted: "Context is wrong, bloated, stale, or missing", starting with Effective context engineering. The aim is to make the right fact retrievable without flooding recall with rows.

## Goals / Non-Goals

Goals: a reader who may read a small dataset's rows finds a fact in them by an ordinary search. No other reader learns from search that a value exists.

Non-goals: changing cards, the vault or Evidence; indexing large datasets; Records and Planning collections; ranking changes beyond normal BM25, trigram and vector scoring; a new tool parameter.

## Decisions

### Index the values under the original's own path

When the indexer reads a dataset card that qualifies (see the bounds below), it also emits one derived page row whose path is the card's `data_file`, the dataset original. The row's body is the value rendering. The row is derived state only: it is rebuilt from the original and the card, and it is never written to the vault.

The row deliberately carries no `emitted_parent`. Parent collapse would fold a value hit into the card, and egress would then judge the card's rule instead of the dataset's. As a standalone row at the original's path, the hit reaches `annotate_hits` as a non-Markdown path. It takes the same path-only decision as `query_dataset`, so the existing release plane gates it with no new authorization code.

A value hit is released only at the full release level. At any lower level the hit is dropped, never degraded to an excerpt, because any excerpt of a value row is raw content. Unresolved membership drops the hit, as it does today for raw reads.

Alternatives rejected:
- Writing values into the card would release them under the card's rule, which can be wider than the dataset's.
- A hidden part page in the vault would mean a new vault artifact class, and its own frontmatter would decide its release.

### Render values as field-path lines, within fixed bounds

The rendering reuses `query_data.load_rows_bytes`, the parser that `query_dataset` uses.
- Each record becomes one block of `field.path: value` lines, one per scalar.
- JSON nesting is flattened to dotted paths, and list indices are kept.
- CSV and TSV use the same first-record-as-header assumption that exact queries state.

A dataset qualifies when all of these hold:
- Its card's inspection status is `ready` or `limited`.
- The original is at most 64 KiB, the existing text-preview budget.
- It parses within the existing 10,000-row limit.

The rendering itself is capped at 64 KiB, and each value at 256 characters. Past a cap the rendering ends with an explicit truncation line. A dataset over these bounds keeps today's behaviour: card only.

The bounds are product limits, not vocabulary. They live as named constants next to the existing inspection budgets in `source_inspection.py`.

### Passages and filters follow existing paths

`find_parts.matched_passage` builds the passage at result time from the rendering. The rendering is recomputed from the immutable original, which is small by construction. The derived row copies the card's filter axes (source kind, tags, project, domain, `file_type`), so structured filters treat it like its card.

### Freshness

The row's content hash combines the card's content hash and the original's recorded `binary_sha256`. A changed card or original rebuilds the row. A tombstoned or deleted card removes it. Background indexing applies the local access-tier exclusion to the original's path before reading it, as it does for every candidate.

## Risks / Trade-offs

- A full-access reader can now get a dataset original as a search hit. This is the intended outcome. The hit names `query_dataset` as the way to read exact rows.
- First-record values of a headerless CSV become searchable for full-access readers only. Those readers can already read them through `query_dataset`.
- Small datasets with many repeated values could crowd results. The 64 KiB cap bounds each dataset to a few BM25 rows' worth of text, and the row collapses to one hit per dataset.

## Migration Plan

No migration. The next index rebuild or incremental reconcile emits rows for existing qualifying datasets. Rolling back removes the rows on the next rebuild, and nothing in the vault changes.
