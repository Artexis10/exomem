## Context

`dataset_values.py` keeps a derived SQLite sidecar of small dataset values with one FTS5 row per record (`value_terms`: `paths`, `words`) and a compact trigram index per record (`value_compact`). A record matches when every query stem is in the record and at least one is in a value. `commands.op_find` fuses every value hit with the page hits by reciprocal rank, and `annotate_hits` releases a value hit only at full level. A hit's passage holds the record's lines within `find_parts.MAX_PASSAGE_CHARS` (4,000), centred on the best field.

A record is a whole single-object JSON file or one CSV row. Field paths are dotted, with list indices kept (`suppliers.0.payment_routes.1.iban`).

## Goals / Non-Goals

Goals: natural phrasings of a lookup find the value. Queries about notes do not pull in diagnostic exports. Value hits stay a small share of a result.

Non-goals: semantic matching of values (a paraphrase that shares no word with the field or its value), any change to release decisions, the hosted projection path, or page ranking.

## Decisions

### Query words

The lane takes its query words from `find_policy.query_word_stem_groups`, the same split that page search uses. One group is one query word with its stems. A group that `query_word_stem_groups` marks as a function word is not required. One-character stems that hold no digit are removed from a group, and a group left empty is dropped. This removes the "s" of a possessive, which the tokenizer splits off. The function-word lexicon in `find_policy` is existing code debt. This change reuses it and adds no new word list.

A group is present in a field when all its stems are in that field's path or value, and each stem may sit in either. It touches a value when at least one of its stems is in the value; a compound like `acme-iban` counts, since its key stem names the value. A group that appears only in field paths still matches nothing, so a query of field names alone finds no hit. The digit rule is stricter: it needs every digit group's stems together in one field's value. For an unspaced run page search counts a word present at a strict majority of its bigrams; this lane requires all of them, which is stricter. That divergence is accepted debt, not a contract: no delta scenario depends on it and no query the vault asks hits it today.

### Weights

Let N be the number of indexed datasets in the request's scope. A group's dataset frequency, df, is the number of those datasets with a field that holds it. The group's weight is `ln(1 + N / (df + 1))`. A group that no dataset holds has the highest weight, so a query that names something not on file does not match on its other words.

### Parts and coverage

A part of a record is the set of fields under one path prefix: the whole record (prefix ""), or every field under `a`, `a.b` and so on, split on ".". Its size is its number of fields. Only parts with at most `MAX_PART_FIELDS` (40) fields count. A CSV row is one part with one field per column.

For each part, the found groups are the groups that any of its fields holds. A group with a stem that holds a digit is required: the part fails unless one of its fields holds every digit-bearing group together in its value. A number is exact, so an account number that differs in one group never matches, even when another field of the record happens to hold the missing group. The part qualifies when at least one found group is in a value, its coverage (found weight / total weight of all groups) is at least `MIN_COVERAGE` (0.5), and it passes the noise gate. The record's score is its best qualifying part: highest coverage, then the fewest fields. A dataset's score is its best record.

Alternatives measured on the same vault:
- Not requiring function words and fragments, and keeping every other word required: 42 of 69 random lookups found, but "test results" still returned 72 value hits.
- Any word, ranked by weight: 21 of 69 found, and note-style queries returned 22 to 227 value hits.
- No part limit (the whole record is one part): the same recall, and about twice the noise on note-style queries.

### Noise gate

When N is at least `NOISE_GATE_MIN_DATASETS` (40), a part fails when the number of datasets with a record that holds all its found groups is more than `max(3, NOISE_GATE_FRACTION × N)`, with a fraction of 0.02. A small scope has no gate: with few datasets, frequency says nothing, and few datasets cannot crowd a result. On the measured vault, with the gate, 9 of 16 note-style queries returned no value hit and the rest returned at most 7 before the cap. All 22 supplier phrasings still found the profile at rank 1.

### Compact matches

The compact trigram lane is unchanged: a query whose letters and digits form a 6 to 64 character run with a digit matches a value that holds the run. A compact match has coverage 1 and a part that is the matched field. The noise gate does not apply to it, because the run already names one identifier. Word matches come before compact-only matches.

### Order and cap

Value hits are ordered by coverage, then part size, then the card's `updated` date, newest first, then path. At most `MAX_VALUE_HITS` (3) go to `fuse`. The cap applies before egress. The fusion limit stays the page limit plus the number of value hits, so a value hit that egress withholds still displaces no page hit.

### Passage

The passage holds whole lines of the record, within `MAX_VALUE_PASSAGE_CHARS` (600). The first line is the first field in record order that holds a found group in the chosen part. The other lines that hold a found group follow in record order. The remaining budget is filled with the lines that come directly after the shown lines, nearest first. A gap between two shown lines is marked with one "…" line. The truncation line and the `query_dataset` line follow as before and do not count toward the budget. The excerpt starts at the first shown line.

### Sidecar schema

One FTS5 row per field replaces one row per record: `value_fields(paths, words)`, with a `fields` table that maps each row to its record, field number and path. `value_compact` stays one row per record. The schema version changes, so the existing versioned rebuild drops and recreates the data tables on the first search after the upgrade. The 64 KiB rendering bound still limits the size of each dataset.

Candidate records are found from the heaviest groups. Sort groups by weight, highest first, and take the shortest prefix whose weight is more than half the total. A record without one of those groups cannot reach coverage 0.5. The lane reads at most `MAX_CANDIDATE_RECORDS` (500) such records, best bm25 first, and then reads the fields of those records for every group.

## Risks / Trade-offs

- A lookup whose words are all common across datasets finds no value hit when the scope has 40 or more datasets. On the measured vault this cost 4 of 69 random lookups. Those lookups used only words that diagnostic exports share.
- An entity named in more than 2% of datasets, such as a supplier with a separate export each month, passes the gate only when the other query words narrow the match. "Acme invoice March" passes and "Acme" alone fails.
- A query that shares no word with a field or its value still misses: "payment details" does not find a field that holds only an IBAN. Semantic value matching is a separate change.
- A dataset whose original carries the raw-protection prefix stays outside value search, as before this change. When the newest version of a profile is protected, the newest unprotected version comes first.
- The numbers 40, 0.5, 0.02 and 3 are product limits, set as named constants with their reasons next to the existing bounds.

## Migration Plan

No migration. The sidecar version changes, and the first search after the upgrade rebuilds it under the existing 24-dataset limit per request. During that window the lane reports `warming: dataset_values` as before.
