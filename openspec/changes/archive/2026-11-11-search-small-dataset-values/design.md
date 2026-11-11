## Context

Search builds its page rows in `lexstore.page_rows` and its embedding chunks in `embeddings._chunks_for_page`, both from a page's `search_body`. A preserved dataset contributes only its card, `<file>.json.md`, whose body holds structural metadata and no values (`source_inspection._inspect_dataset`). Search results pass through `governance/egress.annotate_hits`, which decides each hit by its path. A Markdown hit is judged on its own frontmatter. A non-Markdown path such as a dataset original takes the path-only decision `membership.evaluate_path_only`. Under a still-undecided semantic scope, that decision is unresolved for a new dataset, because its card carries no governance descriptor. `query_dataset` authorizes raw reads with the same path-only decision and requires the full release level.

So the card and the rows can have different read rules. A value copied into the card would be released under the card's rule. That is why the earlier change kept values out of cards, and the reason still holds.

Roster row consulted: "Context is wrong, bloated, stale, or missing", starting with Effective context engineering. The aim is to make the right fact retrievable without flooding recall with rows.

## Goals / Non-Goals

Goals: a reader who may read a small dataset's rows finds a fact in them by an ordinary search. No other reader learns from search that a value exists.

Non-goals: changing cards, the vault or Evidence; indexing large datasets; Records and Planning collections; changing how pages rank; a new tool parameter.

## Decisions

### Keep the values in a separate derived table with its own lane

`dataset_values.py` holds one derived SQLite sidecar in the vault state directory, next to the other derived stores. It has two FTS5 indexes over a small dataset's value rendering, one record per row. `op_find` runs a separate lexical lane over it after `find` returns, and merges the lane's hits into the result with the same reciprocal-rank fusion `find` uses (`config.rrf_k`, page hits first on a tie). The fusion limit is the page limit plus the number of value hits, so a value hit that egress drops later never takes the place of a page hit. The final cut to the caller's `limit` happens after egress. The sidecar is never written to the vault. Dropping it changes cost, never an answer.

A hit has the original's own path, so it reaches `annotate_hits` as a non-Markdown path and takes the same path-only decision that `query_dataset` takes. It has no parent card, so collapse cannot fold it into the card and egress cannot judge the card's rule instead.

Alternatives rejected:
- Writing values into the card would release them under the card's rule, which can be wider than the dataset's.
- A hidden part page in the vault would mean a new vault artifact class, and its own frontmatter would decide its release.
- A synthetic row for the original in the main lexical index. A page row comes from a Markdown page, and the original has none, so the row would need its own filter axes and would sit outside the tombstone and heal paths of every other row. `find` also has deferred-finish, probe, unit, mixed and delta-cache paths, and each would need a fence for the new row kind. The main index's sync would carry a second reconcile rule.

### Release value hits only at full level

`annotate_hits` releases a hit at `RELEASE_FLOOR`, the excerpt level (5). `query_dataset` requires the full level (6). Leaving the gate as it was would hand a reader at level 5 an excerpt of raw rows that `query_dataset` refuses to give them. A hit built by the lane carries a `dataset_value` marker, and `annotate_hits` drops it when the decision is below the full level. The drop is silent, with no notice, because a notice would tell a reader who may not read the rows that a value matched. A hit with no decision (unresolved membership, a missing page) is dropped by the existing rule. The no-governance fast path and the owner decision are unchanged.

The marker is not serialized. Nothing else reads it.

The drop leaves no trace in the rest of the result. The caller's page hits, their order and their count equal what the request returns with the lane off. The warming marker (see below) is hidden from a caller held to content floors, so it cannot reveal that the lane found something either.

### Render values as field-path lines, within fixed bounds

The rendering reuses `query_data.load_rows_bytes`, the parser that `query_dataset` uses.
- Each record becomes a `ValueRecord`: an ordered tuple of `(field path, value)` pairs, one per scalar. Its text block is one `field.path: value` line per pair.
- JSON nesting is flattened to dotted paths, and list indices are kept.
- CSV and TSV use the same first-record-as-header assumption that exact queries state.

A dataset qualifies when all of these hold:
- Its card's inspection status is `ready` or `limited`.
- The card names the original beside it: `data_file` is the card path without `.md`.
- The original is at most 64 KiB, the existing text-preview budget.
- It parses within the existing 10,000-row limit.

The rendering itself is capped at 64 KiB, and each value at 256 characters. When the rendering reaches its cap, later records are not searched, and every hit from that dataset ends its passage with an explicit truncation line. A dataset over these bounds keeps today's behaviour: card only.

The bounds are product limits, not vocabulary. They live as named constants next to the existing inspection budgets in `source_inspection.py`, as do the two status literals that qualify a card.

The card test is the page's own `is_dataset_card` property, which `file_kind` also uses. It names a card by its `data_file` metadata and the structured-source rule, not by an entry key of a vocabulary pack.

### Match values only, by words and by compact form

A header or a path must never make a dataset match: two exports that share a column such as `customer_name` would otherwise fill the result with originals for any query that names it. The sidecar therefore keeps the field paths and the values apart.
- `value_terms` has two columns, `paths` and `words`. Both hold stems under the main lexical tokenizer. A record matches when every query word is in the record and at least one of them is in the values column. A query of column names alone matches nothing. Ranking is bm25.
- `value_compact` is a trigram index over the values only, each value reduced to its letters and digits, casefolded, and joined per value. The lane runs it only when the query's letters-and-digits run is 6 to 64 characters long and holds a digit. "EE81 7700 1234" and "ee81770012" both find the same value. A word such as "customername" never reaches it.

The result holds one hit per dataset, with word matches first.

The passage is built from the record's own lines, whole lines only. The window starts at the line that matched best and fills to the passage budget, `find_parts.MAX_PASSAGE_CHARS`, so a large record shows the matching value and not its first fields. A truncated rendering ends the passage with an explicit line. Every passage ends with a line that names `query_dataset` and this hit's path as `data_file`. The hit carries a `passage` with `text` only. It carries no `matched_part` and no offsets, because the dataset original has no parts and the passage is not a slice of a stored page.

### Build the sidecar at query time and check every hit

One rule keeps the sidecar in step, run when a request reaches the lane:
1. Read the cards in the request's recall projection. Ordinary recall already excludes Records and Planning descendants and applies the access-tier rule to the card. The lane also applies the caller's path admission to the card and to the original, and the access-tier exclusion to the original, before it reads the original. The qualified cards of a scope are kept in memory per process, keyed by the projection key of the snapshot, so a request does not parse every card again. Path admission runs on each request, never from that cache.
2. Index a card that has no row, or whose `card_hash` differs from the stored one. At most 24 datasets are indexed per request. The lane then reports `warming: dataset_values`, and the next request continues.
3. Delete a row whose card left the scope's recall projection.
4. Search only rows built from a card's current hash.

Writers, rebuilds and heals never maintain the sidecar, so no sync path can leave it stale. A rebuild after the original or the card changes needs no event.

The sidecar stores a version that covers its schema and the renderer that produced its rows. When the version differs, one transaction re-checks it, drops the data tables, recreates them and writes the new version, so two requests that meet a stale sidecar do not both rebuild it. The sidecar file is replaced only when SQLite reports it corrupt or not a database. Any other error leaves the file and reports the lane as failed, because a replacement would throw away a good index.

A stored row is never trusted for release. Each hit re-reads the original through the guarded read, checks its SHA-256 against the card's `binary_sha256`, renders it again, and takes the record from that fresh rendering. A changed original with an unchanged card gives no hit.

If FTS5 is unavailable or the sidecar cannot be read, the lane reports `failed: dataset_values`. It never reports an empty result as success.

The lane does not run when the caller asks `explain`. A value hit has no ranking evidence, which `explain` requires of every hit, so the trace reports the lane as disabled with the reason `explain_does_not_trace_dataset_values`.

The hosted projection runtime (`projection_runtime.find_projected_hits`) is a separate read path with its own snapshot and release function. It is not wired to this lane, so a request served from it returns page hits only. A hosted copy needs its own scope and egress design.

## Risks / Trade-offs

- A full-access reader can now get a dataset original as a search hit. This is the intended outcome. The hit names the original, and `query_dataset` reads exact rows.
- First-record values of a headerless CSV become searchable for full-access readers only. Those readers can already read them through `query_dataset`.
- The lane is lexical only. No vector chunk is built, so a paraphrase of a value does not find it. The values are short codes, names and amounts, which lexical matching serves, and a vector chunk would add an embedding cost per dataset and a second place to hold the freshness rule.
- A query matches only when all its words are in one record and at least one is in a value, or when its letters and digits, with at least one digit, form a run in one value. A query with filler words that no record holds gives no value hit, and the dataset is reached through its card if at all. Task 3.3 therefore checks descriptive phrasings as well as the bare number.
- The lane runs for an unfiltered text query at page or auto result level. It does not run for a vector-only search, a structured filter, a trace or the unit and mixed result levels. Its result is recomputed on each request and is not part of `find`'s result cache.
- Small datasets with many repeated values could crowd results. The 64 KiB cap bounds each dataset, and the lane returns at most one hit per dataset.
- The first search after an upgrade indexes at most 24 datasets and reports `warming: dataset_values` until the rest are indexed. The marker carries no count, and a caller held to content floors does not receive it.

## Migration Plan

No migration. The first search after release builds the sidecar from the existing qualifying datasets. Rolling back leaves an unused file in the state directory, and nothing in the vault changes.
