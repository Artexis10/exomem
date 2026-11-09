## Context

Search builds its page rows in `lexstore.page_rows` and its embedding chunks in `embeddings._chunks_for_page`, both from a page's `search_body`. A preserved dataset contributes only its card, `<file>.json.md`, whose body holds structural metadata and no values (`source_inspection._inspect_dataset`). Search results pass through `governance/egress.annotate_hits`, which decides each hit by its path. A Markdown hit is judged on its own frontmatter. A non-Markdown path such as a dataset original takes the path-only decision `membership.evaluate_path_only`. Under a still-undecided semantic scope, that decision is unresolved for a new dataset, because its card carries no governance descriptor. `query_dataset` authorizes raw reads with the same path-only decision and requires the full release level.

So the card and the rows can have different read rules. A value copied into the card would be released under the card's rule. That is why the earlier change kept values out of cards, and the reason still holds.

Roster row consulted: "Context is wrong, bloated, stale, or missing", starting with Effective context engineering. The aim is to make the right fact retrievable without flooding recall with rows.

## Goals / Non-Goals

Goals: a reader who may read a small dataset's rows finds a fact in them by an ordinary search. No other reader learns from search that a value exists.

Non-goals: changing cards, the vault or Evidence; indexing large datasets; Records and Planning collections; changing how pages rank; a new tool parameter.

## Decisions

### Keep the values in a separate derived table with its own lane

`dataset_values.py` holds one derived SQLite sidecar in the vault state directory, next to the other derived stores. It has two FTS5 indexes over a small dataset's value rendering, one record per row. `op_find` runs a separate lexical lane over it after `find` returns, and merges the lane's hits into the result with the same reciprocal-rank fusion `find` uses (`config.rrf_k`, page hits first on a tie). The sidecar is never written to the vault. Dropping it changes cost, never an answer.

A hit has the original's own path, so it reaches `annotate_hits` as a non-Markdown path and takes the same path-only decision that `query_dataset` takes. It has no parent card, so collapse cannot fold it into the card and egress cannot judge the card's rule instead.

Alternatives rejected:
- Writing values into the card would release them under the card's rule, which can be wider than the dataset's.
- A hidden part page in the vault would mean a new vault artifact class, and its own frontmatter would decide its release.
- A synthetic row for the original in the main lexical index. A page row comes from a Markdown page, and the original has none, so the row would need its own filter axes and would sit outside the tombstone and heal paths of every other row. `find` also has deferred-finish, probe, unit, mixed and delta-cache paths, and each would need a fence for the new row kind. The main index's sync would carry a second reconcile rule.

### Release value hits only at full level

`annotate_hits` releases a hit at `RELEASE_FLOOR`, the excerpt level (5). `query_dataset` requires the full level (6). Leaving the gate as it was would hand a reader at level 5 an excerpt of raw rows that `query_dataset` refuses to give them. A hit built by the lane carries a `dataset_value` marker, and `annotate_hits` drops it when the decision is below the full level. The drop is silent, with no notice, because a notice would tell a reader who may not read the rows that a value matched. A hit with no decision (unresolved membership, a missing page) is dropped by the existing rule. The no-governance fast path and the owner decision are unchanged.

The marker is not serialized. Nothing else reads it.

### Render values as field-path lines, within fixed bounds

The rendering reuses `query_data.load_rows_bytes`, the parser that `query_dataset` uses.
- Each record becomes one block of `field.path: value` lines, one per scalar.
- JSON nesting is flattened to dotted paths, and list indices are kept.
- CSV and TSV use the same first-record-as-header assumption that exact queries state.

A dataset qualifies when all of these hold:
- Its card's inspection status is `ready` or `limited`.
- The card names the original beside it: `data_file` is the card path without `.md`.
- The original is at most 64 KiB, the existing text-preview budget.
- It parses within the existing 10,000-row limit.

The rendering itself is capped at 64 KiB, and each value at 256 characters. When the rendering reaches its cap, later records are not searched, and every hit from that dataset ends its passage with an explicit truncation line. A dataset over these bounds keeps today's behaviour: card only.

The bounds are product limits, not vocabulary. They live as named constants next to the existing inspection budgets in `source_inspection.py`.

### Match by words and by compact form

A code such as an account number is often stored in groups and asked for without them, or the reverse. Each record is therefore indexed twice:
- `value_terms` holds the stems of the record under the main lexical tokenizer. A query matches when every query word is in one record, ranked by bm25.
- `value_compact` is a trigram index over each line reduced to its letters and digits, casefolded. A query matches when its own letters and digits, 6 to 64 of them, form a run inside one line. "EE81 7700 1234" and "ee81770012" both find the same value.

The result holds one hit per dataset, with word matches first. The passage is the matching record's block.

### Build the sidecar at query time and check every hit

One rule keeps the sidecar in step, run when a request reaches the lane:
1. Read the cards in the request's recall projection. Ordinary recall already excludes Records and Planning descendants and applies the access-tier rule to the card. The lane also applies the caller's path admission to the card and the access-tier exclusion to the original before it reads the original.
2. Index a card that has no row, or whose `card_hash` differs from the stored one. At most 24 datasets are indexed per request. The lane then reports `warming: dataset_values`, and the next request continues.
3. Delete a row whose card left the scope's recall projection.
4. Search only rows built from a card's current hash.

Writers, rebuilds and heals never maintain the sidecar, so no sync path can leave it stale. A rebuild after the original or the card changes needs no event.

A stored row is never trusted for release. Each hit re-reads the original through the guarded read, checks its SHA-256 against the card's `binary_sha256`, renders it again, and takes the record from that fresh rendering. A changed original with an unchanged card gives no hit.

If FTS5 is unavailable or the sidecar cannot be read, the lane reports `failed: dataset_values`. It never reports an empty result as success.

## Risks / Trade-offs

- A full-access reader can now get a dataset original as a search hit. This is the intended outcome. The hit names the original, and `query_dataset` reads exact rows.
- First-record values of a headerless CSV become searchable for full-access readers only. Those readers can already read them through `query_dataset`.
- The lane is lexical only. No vector chunk is built, so a paraphrase of a value does not find it. The values are short codes, names and amounts, which lexical matching serves, and a vector chunk would add an embedding cost per dataset and a second place to hold the freshness rule.
- A query matches only when all its words are in one record, or when its letters and digits form a run in one line. A query with filler words that no record holds gives no value hit, and the dataset is reached through its card if at all.
- The lane runs for an unfiltered text query at page or auto result level. It does not run for a vector-only search, a structured filter, a trace or the unit and mixed result levels. Its result is recomputed on each request and is not part of `find`'s result cache.
- Small datasets with many repeated values could crowd results. The 64 KiB cap bounds each dataset, and the lane returns at most one hit per dataset.
- The first search after an upgrade indexes at most 24 datasets and reports `warming: dataset_values` until the rest are indexed.

## Migration Plan

No migration. The first search after release builds the sidecar from the existing qualifying datasets. Rolling back leaves an unused file in the state directory, and nothing in the vault changes.
