## Context

The lexical lanes all read one tokenizer, `bm25.tokenize`: the FTS5 catalogue (`lexstore`), the in-process `rank_bm25` rung, find's stem gates and coverage, the unit lane, governed projected retrieval, referent attributes and activation's lexical evidence. It keeps `[a-z0-9]+` and English Snowball. The catalogue stores pre-stemmed text in `fts USING fts5(stemmed)`, whose default `unicode61` folds diacritics and splits at combining marks. That was harmless while tokens were ASCII, and would silently break the invariant that FTS and the in-process scorer see identical tokens once they are not.

## Goals / Non-Goals

**Goals:**

- Working keyword recall for every script, with embeddings off, on any install.
- Byte-identical tokens, index and scores for ASCII text, and for English text whose non-ASCII characters are punctuation, symbols, spaces or format characters. English tokens change only around non-ASCII letters and marks (é, full-width letters, ligatures) and numbers that NFKC turns into digits (², ½, Ⅳ).
- No model and no new dependency in the lexical half.
- Dense recall across languages with one multilingual encoder on a personal server, shared with activation, without losing the English golden gate, and a migration during which dense recall never goes dark.

**Non-Goals:**

- Language detection, dictionaries or word segmentation.
- Stemming for Latin languages other than English. German, Estonian and Finnish inflections match only in their exact form here; the dense half covers morphology, and a vault-declared Latin stemmer is a later lever.
- Activation's own index terms (`working_set_index.tokens_of`), which keep their rules.
- A multilingual encoder on hosted or cloud cells: they keep the English model until a node encoder serves them.
- Language detection for the rerank gate (D12).

## Decisions

### D1. Script-keyed tokens, no language detection

ASCII text keeps the v1 regex path and is untouched. In other text, every non-ASCII symbol (S*) and enclosing mark becomes a separator and variation selectors are dropped, before NFKC normalisation and casefolding; otherwise NFKC would turn "Zorblex™" into `zorblextm` and "20℃" into `20c`. A token then starts with a letter or number and continues with letters, numbers and combining marks; a mark with no base never starts one ("it´s", an overline that NFKC decomposes into a space and a mark). Underscore and all punctuation separate, as `[a-z0-9]+` always did, so every punctuation, symbol, space and format code point glued between ASCII words tokenizes exactly as under v1. Numbers that NFKC turns into digits (², ½, Ⅳ) stay in the word they touch: they are numbers, not separators. A token splits again where it crosses between an unspaced script and a spaced one.

An unspaced run emits overlapping bigrams of characters, where a character is a base plus its combining marks. A one-character run emits that character. A spaced word is stemmed by the script of its letters.

Why not detection: a query is short, so detecting its language is unreliable, and a query stem that differs from the index stem of the same word loses the match. Pages mix languages, and a detector's version drift would silently re-stem the index. Script-keyed stems cannot disagree between query and index, because they are the same function of the same string.

The unspaced-script table is declared Unicode block ranges in `text_scripts.py`. It is data about scripts, not a word list, and any later rule that needs to know whether text is unspaced (CJK containment in activation, chunk caps for unspaced paragraphs) reads the same table.

### D2. Accent fold on the index side only

If removing combining marks (NFD, drop `Mn`, NFC) changes a word whose letters are all Latin, the index also stores the folded form's stem. "Zölvarn" stores `zölvarn` and `zolvarn`; "résumé" stores `résumé` and `resum`.

The query side emits surface forms only. A query typed without accents matches the folded variant, and a query typed with accents matches the exact surface. No query term is counted twice, and an accented query never widens to unaccented pages.

The fold is restricted to Latin because dropping marks destroys Indic words ("हिन्दी" would become "हिनदी") and conflates Cyrillic й with и.

### D3. Units, not stems, for corroboration

`token_units` returns the stems grouped by the word or run they came from. Activation's lexical evidence requires two corroborating matches. Counted over stems, one accented word (surface plus fold) or one Japanese sentence (many bigrams) would be its own second vote. The catalogue's corroboration filter therefore maps every stem to the first query unit it came from and counts distinct units. A word unit counts when any of its stems is on the page. A run unit counts only when a strict majority of its content bigrams (D4) are: otherwise "明日は、散歩です" would corroborate a page about today's weather through the particle bigrams 日は and です alone. On ASCII every unit is one stem, so the count equals v1's count of distinct stems.

### D4. Find's word coverage treats an unspaced run as one word

Find's degraded-retention gate and its all-words veto count whole query words. A whitespace word keeps its v1 rule: present when every subtoken stem is present, so exact-marker compounds stay precise. An unspaced run is its own word, present when a strict majority of its content bigrams occur in the page. Its content bigrams are those without hiragana, the script Japanese writes particles and inflections in, and without the question kanji 何, which builds question words with a counter (何度, 何時, 何月) that ask rather than name. A run with no such bigram keeps its hiragana-free bigrams, a run whose every bigram holds hiragana keeps them all, and runs in other scripts keep every bigram.

- Requiring every bigram, or a majority of all of them, would demand the query's particles: "会議の議事録はいつ共有" would miss a page that says "議事録は翌日までに共有します", and "抹茶の用意" would miss "抹茶と和菓子は講師が用意します".
- Accepting any one bigram would retain a page that shares a single particle bigram.
- A query made only of particles ("についてですか") has no content and finds nothing.
- The trade-off: a content word written in hiragana ("りんごの値段") drops out of its run's content, so only 値段 is required and a page about banana prices is retained. It touches only whether a lexically matched candidate is kept (find's retention gates) or corroborates (D3); BM25 still ranks on every bigram, so the page naming りんご ranks above it, and vector-ranked candidates skip the gates.

Rerank's word count counts whitespace words as before, and each unspaced run inside a non-ASCII word as a word.

### D5. Catalogue declaration and rebuild

`fts` and `unit_fts` are declared `tokenize="unicode61 remove_diacritics 0 categories 'L* N* Co M*'"`. Probed on SQLite 3.53.1, every NFKC-casefolded letter, number and mark round-trips as its own token (0 mismatches over 140,675 code points).

`SCHEMA_VERSION` moves from 10 to 11. A v10 catalogue reads not-current until the existing atomic background rebuild publishes. Meanwhile `find` answers `RETRIEVAL_INDEX_WARMING` on a vault of more than 64 pages or under a managed runtime (a smaller unmanaged vault rebuilds inline); `find` does not fall back to the in-process rung, which serves only direct BM25 callers and the `EXOMEM_LEXICAL_BACKEND=python` kill switch. Activation reports `stale`. The in-place rebuild path re-declares FTS tables whose declaration predates v2; `CREATE ... IF NOT EXISTS` alone would refill them under the old tokenizer.

The version bump sends every install through a rebuild, and a killed rebuild leaves a whole catalogue behind as a temp file. Each rebuild therefore starts by removing rebuild temps, with their WAL and SHM, that are orphans. A build holds an advisory lock on its temp, in the user's private lock directory, from before the temp exists until it is cleaned up, and the OS releases it when the build's process dies. A temp is an orphan when no build holds its lock, it has been untouched for ten minutes, and no connection holds it open. The lock is what protects a live build that has closed its connection or stalled, or whose mtimes look old after a clock jump.

### D6. English non-regression is measured, not assumed

- A property test compares v2 with a frozen copy of v1 over 20,000 random ASCII strings in both modes, and over 30,000 more with one random non-ASCII punctuation, symbol, space or format character each, across the fast-path/scanner boundary. Every such code point glued between ASCII words tokenizes as under v1.
- The golden fixture's pages tokenize identically under v1 and v2, and its golden queries rank identically on both lexical backends.
- With sixty German, Russian, Japanese and Estonian pages added, no golden page moves by more than one rank and NDCG@10 moves by at most 0.01.

LongMemEval is not part of this gate.


### D7. One model: `BAAI/bge-m3` on ONNX Runtime int8

A personal server encodes recall with `BAAI/bge-m3`, served from its pinned int8 artefact on CPU, one text per run, and activation shares the one resident instance. The model decision was taken on the golden set, the multilingual fixture and the Japanese-only vault, with bge-base as the control (2026-09-23, shared host):

| Model / path | NDCG@10 | recall@10 | MRR | padded NDCG (Δ) | query p50/p95 ms (load) |
|---|---|---|---|---|---|
| bge-base (control) | 0.9326 | 0.9655 | 0.9241 | 0.9328 (+0.000) | 59/72 (14.0) |
| bge-m3 ORT int8 | 0.9311 | 0.9483 | 0.9241 | 0.9316 (+0.001) | 42/57 (22.2) |

bge-m3 int8 clears the English gate (NDCG@10 ≥ 0.897, recall@10 ≥ 0.9415, no query at recall 0, no query losing its best page) and every multilingual bar but the language-bias twins, which every model failed before the fusion rule in D11. The recall@10 drop is one query (see Risks). bge-base scored 0 on every cross-language row; bge-m3 scores 1.

A hosted or cloud cell keeps `BAAI/bge-base-en-v1.5` until a node encoder serves it: about 0.6-0.7 GB per process is over the per-cell budget. `EXOMEM_RECALL_MODEL` names the model explicitly; CI's hosted timing gate pins the English model it certifies.

### D8. A sidecar records its vector space

Every recall store records the encoder that wrote it (`meta` keys `embedding_model`, `embedding_fingerprint`, `embedding_dim`): the model, the resident encoder's fingerprint (for a served model its revision, quantisation, file format and artefact digest), and the width. Fingerprints are compared exactly when both are known; a record without one (written before profiles, or by a test substitute) matches by model name, and its width must still match. A sidecar with rows and no record is `bge-base-en-v1.5` at 768 dimensions, the only encoder recall ever shipped, and its record is written by its next write.

The width is read from the record everywhere: the matrix, vec0's column (redeclared when the width changes), stored-vector reuse, semantic units, claims, warm-up and the projected catalogue. Encoding for a sidecar happens inside `EmbeddingIndex.encoding()`, which checks the encoder against the record before anything is encoded; the vector lane is otherwise `unavailable` with `vector_space_mismatch` and the other lanes serve. Claims record their space too and re-encode under a new one.

### D9. Blue/green re-embed

The serving sidecar is named by an active pointer (`.embeddings.active`) beside it; without one `.embeddings.sqlite` serves, so a fresh install and a hosted cell keep the legacy name. A new space is built in `.embeddings.<16 hex>.sqlite`, named from the target fingerprint. Both names are reserved for the embedding index.

While the build runs, the serving sidecar keeps serving with the encoder that wrote it: a second encoder, loaded by warm-up in every mode before writes are admitted, or by the job, never by a request, released at the cutover and not reaped. A request that finds it cold reports the vector lane `warming`. A cell never runs a second encoder: a sidecar of another model is refused there.

The job (`recall_migration`) runs beside the other service workers, never on a cell. It builds in committed batches through the live chunking seam, one text per encode (a shared int8 batch moves a vector by up to 0.02 cosine; one text per encode also bounds how long a query waits for the build to one passage). A page is done when its new rows carry its current mtime, so a restart re-encodes nothing built and a page written during the build is simply encoded again. The cutover runs catch-up passes, replaces the pointer in one atomic write, takes any write that landed in between and releases the old encoder. A failed cutover leaves the old sidecar serving. The old sidecar is removed only by a later start of the job, after one catch-up pass over the serving sidecar, never by the process that cut over. `EXOMEM_RECALL_REEMBED=off` builds nothing.

Doctor's `embeddings.reembed` check reads the sidecars on disk (serving space, pages built); `exomem status` reports the running job's rate and estimate. `observe_memory` in this product mutates semantic units and has no status surface, so the progress lives in those two.

### D10. Chunks stay under the encoder's limit

The 350-word cap stays, so English chunk boundaries are unchanged. A paragraph the word cap cannot bound (mostly unspaced text, more unspaced text than the cap allows, or a whitespace word longer than the character cap) splits into pieces of at most 500 characters: at a sentence end, else the last space, else the cap off any combining mark. No chunk exceeds 512 bge-m3 tokens.

### D11. The dense-lead guard withholds by script

When the vector lane's first candidate shares no content word with the query, the lexical lanes are blind to it, and reciprocal-rank fusion let a same-language page that matched one or two words outrank it. A lexical lane therefore withholds its vote from a partial match whose dominant letter script differs from the lead's. No count threshold: a same-script page never loses a vote, so English cannot move (0 of 29 golden top-10 lists changed on four harness corpora, and the synonym query that shares no word with its answer ranks as before). The guard reads only candidates that can reach the fused window, is timed as its own stage, and records every withheld vote and its reason in the explain trace and lane status. Governed projected recall applies it too.

The German and Estonian twins share the English page's script, so the guard does not act there and the encoder decides. On the acceptance fixture (three twins per language) the English gold is in the top 10 of every twin and outranks the poison in every Russian and Japanese twin, in 2 of 3 German twins and in 1 of 3 Estonian twins, and every lost twin loses by exactly one rank. A gold outside the top 10 fails the bar outright. The bar, the gold winning most of a language's twins in at least three of four languages and never losing more than one rank, holds with German, Japanese and Russian; it is met by the script rule and the encoder, not by a tuned count.

### D12. Rerank coverage by script, not language

Each declared reranker states the scripts it reads and whether it reads across languages: `bge-reranker-base` {Latin, Han}, not cross-lingual; `bge-reranker-v2-m3` all scripts and cross-lingual, both assumptions from its model card, not measured here, as are its memory and latency estimates. A rerank is skipped when the query's dominant script is outside the declared set, or when fusion reports a crossing (votes withheld across scripts, or an unmatched query in another script than the lead) and the reranker is not cross-lingual. An undeclared reranker is not gated.

The design asked for a language gate, because `bge-reranker-base` hurts cross-language German and Estonian queries. Telling German or Estonian from English needs a language-detection model; the script signal does not. So German and Estonian queries over an English lead are still reranked: a stated gap.

### D13. Acceptance and measured cost

`tests/test_recall_multilingual_embeddings.py` gates the switch in the embeddings job through `find` (hybrid, rerank off), on trees built by the product writers. Measured on a shared 16-core host at load average 8-9 (2026-09-25):

| Arm | Measured | Bar |
|---|---|---|
| English golden NDCG@10 / recall@10 / MRR | 0.9312 / 0.9483 / 0.9241 | ≥ 0.897 / ≥ 0.9415 / — |
| Golden padded with the multilingual and twin pages, NDCG@10 | 0.9312 (Δ +0.0000) | within 0.02 |
| Same-language recall@5, de / et / ja / ru | 1.00 each | ≥ 0.90 |
| Cross-language recall@10, de / et / ja / ru (gold rank 3 / 3 / 1 / 1) | 1.00 each | ≥ 0.80 |
| Morphology recall@10, et / ru | 1.00 each | ≥ 0.80 |
| Twins won (most of a language's three) | de, ja, ru | ≥ 3 of 4 languages, never more than one rank lost |
| Japanese-only vault (27 queries), hybrid recall@10 | 1.00 | ≥ 0.95 |
| Short-query encode p50 / p95 | 30 / 43 ms | p95 ≤ 250 ms |
| The same during a one-text-at-a-time build | 75 / 178 ms | reported |
| Build, seconds per 1,000 chunks (golden / padded / Japanese) | 69 / 60 / 54 | reported |
| The one instance: load, max RSS added | 2.2 s, +588 MiB | reported |

The same test on the English model's tree fails: cross-language recall 0 in every language and no language wins its twins.

LongMemEval is not part of any gate.

## Risks / Trade-offs

- **Postings grow for unspaced text.** One token per character against about one per five for English, so a Japanese vault's catalogue grows about 5× per byte of text. FTS5 handles it; the rebuild time on a 3,000-page fixture is recorded in tasks.md.
- **Corpus statistics move in mixed vaults.** Only pages with non-ASCII letters, marks or NFKC-digit numbers change their tokens, which moves average length and document frequency. This is bounded by the padded golden arm.
- **Typographic English matches more.** A query word written with a curly apostrophe or an em dash ("what’s", "harbor—crane") was stemmed whole under v1 and never met a page; it now splits exactly as the ASCII apostrophe or hyphen always did. This is intended.
- **The scanner costs more than the ASCII path.** A 6.5 KB English page with typographic dashes tokenizes in about 1.6× the ASCII path's time, because only the non-ASCII stretches are normalised and scanned.
- **No stemming for non-English Latin languages.** Accepted, as above.
- **A CJK turn earns no lexical `retrieval` in activation.** One run is one unit, and a single run never corroborates. That is conservative, and semantic evidence is its second contact.
- **The CJK carry stays off.** The carry reads rarity on the turn's surface forms and pairs two distinctive stems only from two different words, so an accented word and its folded variant never make a phrase with themselves, and the parts of a joined compound still pair. An unspaced run (Han, kana, Hangul, Thai and the like) contributes no pairable stem, so a turn in those scripts is never carried and runs no ranking query. This is a stated limit: two runs share particles and endings with every page in their script ("明日は、散歩です" carried a weather note on "日は" and "です"), the pair groups grow as the product of the two runs' bigrams (a 1,200-character Japanese turn took about a minute on a 1,600-page vault), and pairing them would need a position model over character offsets.
- **One golden typed-graph neighbour leaves the top 10 under bge-m3.** For "advanced mode reveals information but does not change the safety model" the grade-3 page stays first, but its grade-1 `relates_to` neighbour ranks 12th in fusion. That one query is why golden recall@10 is 0.9483 against bge-base's 0.9655; every English bar holds. The graph lane still expands the edge and the neighbour still enters fusion, which the golden typed-graph tests pin. Lifting typed neighbours in fusion is a follow-up that needs its own English no-change check.
- **A re-embed holds two encoders, in every mode.** While a vault built by the English model migrates, the English encoder serves its sidecar beside bge-m3 until the cutover releases it. Warm-up loads it in performance, normal and quiet mode alike, before writes are admitted, so quiet mode also runs the re-embed with two encoders resident; `EXOMEM_RECALL_REEMBED=off` keeps only the English one serving and builds nothing. On the owner's vault the build is estimated at 10-20 h of background CPU.
- **A rollback after retirement needs a full re-embed.** A later start removes the English sidecar once the new one serves. Going back to `bge-base-en-v1.5` after that (`EXOMEM_RECALL_MODEL`, or an older release) re-embeds the whole vault into the English space, with the same background build and cutover.
- **A crash between the pointer swap and the pass after it.** A write that landed in the old sidecar in that window is caught up by the next start's pass over the serving sidecar, which runs before the old sidecar is retired.
- **A query vector names its space.** A plan that reuses one query vector across lanes checks it against each sidecar it searches, so a cutover between the lanes reports the vector lane `unavailable` instead of mixing two spaces of one width.
- **The rerank gate reads scripts, not languages.** German and Estonian queries over an English lead are still reranked by `bge-reranker-base`, which hurts them (D12).
- **Hosted keeps the English model.** About 0.6 GB per process is over the per-cell budget; hosted multilingual recall waits for the node encoder.
