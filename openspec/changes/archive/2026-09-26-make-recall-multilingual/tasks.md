## 1. Lexical half: tokenizer v2 (lane R-LEX)

- [x] 1.1 Red first: `tests/test_bm25_unicode_tokenizer.py` pins ASCII identity against a frozen v1 over 20,000 random strings in both modes and across the fast-path/scanner boundary, the golden fixture pages, CJK bigrams, the Latin-only index-side fold, Russian/Greek/Armenian stems, Devanagari and Thai kept whole, NFKC forms and `token_units`
- [x] 1.2 `bm25.py` tokenizer v2 with the ASCII fast path, index and query sides, `token_units`, and the declared script table in `text_scripts.py`
- [x] 1.3 `lexstore.py`: FTS5 declaration with the unicode61 categories and no diacritic removal, `SCHEMA_VERSION` 11, stale declarations re-declared on the in-place rebuild, query-side tokens and unit-counted corroboration. Tests in `tests/test_lexstore.py`: fts5vocab round trip, in-place and atomic rebuild of a v10 catalogue, a CJK query, the accent fold, not-current fallback to the in-process rung, corroboration by units
- [x] 1.4 Every tokenizer caller reads its side explicitly: find's coverage gates, excerpt anchoring and unit lane, `should_rerank`'s word count, governed projected retrieval, referent attributes, the in-process BM25 rung and activation's lexical evidence. Each gets CJK and accent-fold tests beside its existing tests
- [x] 1.5 English non-regression (`tests/test_recall_multilingual_lexical.py`): the golden fixture ranks identically under v1 and v2, and the golden fixture plus sixty multilingual pages moves no golden page by more than one rank, on both lexical backends
- [x] 1.6 Multilingual lexical acceptance: `benchmarks/epistemic/corpora/recall_multilingual.py` with `tests/golden/queries_multilingual.yaml`, and the Japanese-only vault `benchmarks/epistemic/corpora/recall_japanese_vault.py`, with the fixture test `tests/test_recall_multilingual_fixtures.py`
- [x] 1.7 Measure the catalogue rebuild time on a 3,000-page generic fixture (recorded below)
- [x] 1.8 After batch 1 and step-4 T4 are on the base: apply the unit rule to activation's carry. `content_stems` reads the turn's surface forms (query side), and `adjacent_rare_pairs` pairs two distinctive stems only from two different words, splitting a joined token into its parts first; an unspaced run contributes no pairable stem, so the CJK carry stays off as a stated limit. `tests/test_working_set_carry.py` covers an accented word that never pairs or carries, a joined accented compound that still pairs, Japanese and Korean turns whose runs share only particles and endings with a page and carry nothing, and a turn in an unspaced script that never reaches the ranking query. The memory-loop corroboration wording in `close-memory-loop` now counts word and run units, and its carry wording pairs words only
- [x] 1.9 Review round 2: a mark never starts a token; non-ASCII symbols and enclosing marks separate before NFKC and variation selectors are dropped, with every punctuation, symbol, space and format code point pinned against v1; a run is present on a majority of its content bigrams (no hiragana) in find's gates and counts as a corroborating unit only on that majority; orphan rebuild temps older than ten minutes are swept at rebuild start; plain English stretches skip unit objects

Rebuild time, 3,000 generated pages, `rebuild_atomic`, two interleaved runs each on a shared 16-core host at load average 22-26 (2026-09-23):

| Fixture | v1 | v2 | Catalogue size v1 → v2 |
|---|---|---|---|
| English, one typographic dash per page (scanner path) | 10.8 s, 9.5 s | 13.3 s (first, cold), 11.0 s | 15.1 MB → 15.1 MB |
| Mixed: a quarter each English, German, Russian, Japanese | 9.1 s, 9.4 s | 9.4 s, 9.3 s | 14.9 MB → 19.4 MB |

## 2. Dense half: one multilingual encoder (lane R-DENSE)

- [x] 2.1 Recall spike and model decision against the English and multilingual recall gates, recorded in design.md (D7: `BAAI/bge-m3` on ONNX Runtime int8, one instance shared with activation)
- [x] 2.2 Encoder profile declarations (shared with the activation lane: revision, quantisation, file format and artefact digest in the fingerprint, `max_seq` 512), a sidecar that records its vector space with its width read from that record (`tests/test_embedding_index_fingerprint.py`), and unspaced-text chunking under the encoder limit (`tests/test_embeddings_chunking.py`)
- [x] 2.3 Interactive and background encode lanes on one model, and the blue/green re-embed with its active pointer, resumable build, catch-up, atomic cutover, later retirement and `EXOMEM_RECALL_REEMBED=off` (`tests/test_embedding_migration.py`)
- [x] 2.4 The recall switch: bge-m3 on a personal server, the English model on a hosted or cloud cell, `EXOMEM_RECALL_MODEL`, traces and doctor naming the model whose vectors served (`tests/test_recall_switch.py`)
- [x] 2.5 The dense-lead guard in `find` fusion (`tests/test_find_dense_lead_guard.py`) and the rerank script-coverage gate (`tests/test_reranker_coverage.py`)
- [x] 2.6 Dense and hybrid acceptance in the embeddings job (`tests/test_recall_multilingual_embeddings.py`, numbers in design.md D13), the typed-graph golden pins moved from the fused rank to the graph lane and fusion for the one query bge-m3 ranks 12th, the embeddings job's cache key and 45-minute limit, and the dense requirements in this change's spec

Follow-up, not in this change: lift typed-graph neighbours in fusion so the golden "advanced mode" neighbour returns to the top 10, with its own English no-change check.
