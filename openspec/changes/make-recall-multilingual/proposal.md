## Why

Recall is English-only below the encoder. `bm25.tokenize` keeps only `[a-z0-9]+` and stems with English Snowball, so:

- a Japanese, Chinese, Korean or Thai page yields no lexical tokens at all, and a Japanese-only vault with embeddings off finds nothing by keyword ranking;
- a Russian or Greek word never meets its own inflections;
- an accented Latin word splits at its accent ("Zölvarn" indexes as `z` + `lvarn`), so a query typed without the accent misses it, and one accented word counts as two corroborating words.

The lexical half needs no model. It can land on every install, lean or hybrid, personal or hosted.

## What Changes

- **Tokenizer v2**, the single function behind `bm25.tokenize`:
  - ASCII text keeps the v1 fast path byte for byte.
  - Other text has its non-ASCII symbols separated and its variation selectors dropped, is NFKC-normalised and casefolded, and is split into tokens that start with a letter or digit and continue with letters, digits and marks.
  - Unspaced scripts (Han, kana, Hangul, Thai, Lao, Khmer, Myanmar, declared as Unicode blocks in one module) emit overlapping character bigrams.
  - A spaced word is stemmed by its script, never by language detection: English Snowball for ASCII, Russian for Cyrillic, Greek for Greek, Armenian for Armenian, and no stemming otherwise.
  - On the index side only, a Latin word whose accents fold away also emits the folded form.
- **Token units.** `bm25.token_units` groups stems by the word or run they came from. Corroboration counts units, so an accented word or one CJK run is one piece of evidence, and a Japanese run counts only when most of its content bigrams (those without hiragana) are on the page.
- **The lexical catalogue** declares its FTS5 tables so they store these tokens verbatim, and bumps its schema to 11. Every existing catalogue is rebuilt by the existing background rebuild.
- **Every tokenizer caller** reads the query side or the index side explicitly: find's coverage gates, excerpt anchoring, the unit lane, rerank's word count, governed projected retrieval, referent attributes and activation's lexical evidence.
- **Acceptance fixtures**: a multilingual golden set beside the English golden fixture, and an invented Japanese-only vault built through the product writers.

The dense half (one multilingual encoder for recall and activation, the fingerprinted sidecar and its blue/green migration) follows in this change after its model decision. It adds its own requirements then.

## Capabilities

### New Capabilities

- `multilingual-recall`: the lexical token contract, script-keyed stemming, the index-side accent fold, unit counting, the catalogue declaration and rebuild, and the lexical acceptance bars.

### Modified Capabilities

None in the lexical half. `close-memory-loop`'s memory-loop delta, still active, now states its lexical corroboration over word and run units, matching this change's spec, and its carry pairing over words only: the CJK carry stays off.

## Impact

- **Code:** `bm25.py`, new `text_scripts.py`, `lexstore.py`, `find.py`, `find_policy.py`, `find_results.py`, `referent_resolution.py`, `governance/projected_retrieval.py`, and activation's carry in `working_set_runtime.py`.
- **State:** one background rebuild of each vault's lexical catalogue on upgrade, which also removes rebuild temps a killed build left behind. Until it publishes, `find` answers `RETRIEVAL_INDEX_WARMING` on a vault of more than 64 pages (a smaller unmanaged vault rebuilds inline), and activation's lexical stage reports `stale`, as for any rebuild.
- **English:** an all-ASCII vault gets identical tokens, index and scores, and punctuation, symbols, spaces and format characters separate exactly as before. English tokens change only where the text holds non-ASCII letters or marks, or numbers such as ² and ½ that NFKC turns into digits. The golden fixture ranks identically on both lexical backends.
- **No model, no new dependency.** Snowball's Russian, Greek and Armenian stemmers ship in the existing `snowballstemmer` package. Nothing is default-on that was off.
