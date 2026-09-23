## 1. Lexical half: tokenizer v2 (lane R-LEX)

- [x] 1.1 Red first: `tests/test_bm25_unicode_tokenizer.py` pins ASCII identity against a frozen v1 over 20,000 random strings in both modes and across the fast-path/scanner boundary, the golden fixture pages, CJK bigrams, the Latin-only index-side fold, Russian/Greek/Armenian stems, Devanagari and Thai kept whole, NFKC forms and `token_units`
- [x] 1.2 `bm25.py` tokenizer v2 with the ASCII fast path, index and query sides, `token_units`, and the declared script table in `text_scripts.py`
- [x] 1.3 `lexstore.py`: FTS5 declaration with the unicode61 categories and no diacritic removal, `SCHEMA_VERSION` 11, stale declarations re-declared on the in-place rebuild, query-side tokens and unit-counted corroboration. Tests in `tests/test_lexstore.py`: fts5vocab round trip, in-place and atomic rebuild of a v10 catalogue, a CJK query, the accent fold, not-current fallback to the in-process rung, corroboration by units
- [x] 1.4 Every tokenizer caller reads its side explicitly: find's coverage gates, excerpt anchoring and unit lane, `should_rerank`'s word count, governed projected retrieval, referent attributes, the in-process BM25 rung and activation's lexical evidence. Each gets CJK and accent-fold tests beside its existing tests
- [x] 1.5 English non-regression (`tests/test_recall_multilingual_lexical.py`): the golden fixture ranks identically under v1 and v2, and the golden fixture plus sixty multilingual pages moves no golden page by more than one rank, on both lexical backends
- [x] 1.6 Multilingual lexical acceptance: `benchmarks/epistemic/corpora/recall_multilingual.py` with `tests/golden/queries_multilingual.yaml`, and the Japanese-only vault `benchmarks/epistemic/corpora/recall_japanese_vault.py`, with the fixture test `tests/test_recall_multilingual_fixtures.py`
- [x] 1.7 Measure the catalogue rebuild time on a 3,000-page generic fixture (recorded below)
- [ ] 1.8 After batch 1 and step-4 T4 are on the base: apply the unit rule to activation's carry (`content_stems`, `rare_turn_terms` on the surface form, `adjacent_rare_pairs`, `_bounded_corroboration_terms`), with `tests/test_working_set_carry.py` covering an accented word and a CJK run that never pair and hyphenated compounds that still do, and reconcile the memory-loop corroboration wording
- [x] 1.9 Review round 2: a mark never starts a token; non-ASCII symbols and enclosing marks separate before NFKC and variation selectors are dropped, with every punctuation, symbol, space and format code point pinned against v1; a run is present on a majority of its content bigrams (no hiragana) in find's gates and counts as a corroborating unit only on that majority; orphan rebuild temps older than ten minutes are swept at rebuild start; plain English stretches skip unit objects

Rebuild time, 3,000 generated pages, `rebuild_atomic`, two interleaved runs each on a shared 16-core host at load average 22-26 (2026-09-23):

| Fixture | v1 | v2 | Catalogue size v1 → v2 |
|---|---|---|---|
| English, one typographic dash per page (scanner path) | 10.8 s, 9.5 s | 13.3 s (first, cold), 11.0 s | 15.1 MB → 15.1 MB |
| Mixed: a quarter each English, German, Russian, Japanese | 9.1 s, 9.4 s | 9.4 s, 9.3 s | 14.9 MB → 19.4 MB |

## 2. Dense half: one multilingual encoder (lane R-DENSE, follows)

- [ ] 2.1 Recall spike and model decision against the English and multilingual recall gates, recorded in design.md
- [ ] 2.2 Encoder profile declarations, a fingerprint-named recall sidecar with its dimension read from metadata, and unspaced-text chunking
- [ ] 2.3 Interactive and background encode lanes on one model, and the blue/green re-embed with its atomic cutover
- [ ] 2.4 Dense and hybrid acceptance in the embeddings job, the rerank script-coverage gate, and the dense requirements in this change's spec
