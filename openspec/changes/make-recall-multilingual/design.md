## Context

The lexical lanes all read one tokenizer, `bm25.tokenize`: the FTS5 catalogue (`lexstore`), the in-process `rank_bm25` rung, find's stem gates and coverage, the unit lane, governed projected retrieval, referent attributes and activation's lexical evidence. It keeps `[a-z0-9]+` and English Snowball. The catalogue stores pre-stemmed text in `fts USING fts5(stemmed)`, whose default `unicode61` folds diacritics and splits at combining marks. That was harmless while tokens were ASCII, and would silently break the invariant that FTS and the in-process scorer see identical tokens once they are not.

## Goals / Non-Goals

**Goals:**

- Working keyword recall for every script, with embeddings off, on any install.
- Byte-identical tokens, index and scores for ASCII text, and for English text whose non-ASCII characters are punctuation, symbols, spaces or format characters. English tokens change only around non-ASCII letters and marks (é, full-width letters, ligatures) and numbers that NFKC turns into digits (², ½, Ⅳ).
- No model and no new dependency in the lexical half.

**Non-Goals:**

- Language detection, dictionaries or word segmentation.
- Stemming for Latin languages other than English. German, Estonian and Finnish inflections match only in their exact form here; the dense half covers morphology, and a vault-declared Latin stemmer is a later lever.
- Activation's own index terms (`working_set_index.tokens_of`), which keep their rules.
- The dense half, which follows in this change.

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

## Risks / Trade-offs

- **Postings grow for unspaced text.** One token per character against about one per five for English, so a Japanese vault's catalogue grows about 5× per byte of text. FTS5 handles it; the rebuild time on a 3,000-page fixture is recorded in tasks.md.
- **Corpus statistics move in mixed vaults.** Only pages with non-ASCII letters, marks or NFKC-digit numbers change their tokens, which moves average length and document frequency. This is bounded by the padded golden arm.
- **Typographic English matches more.** A query word written with a curly apostrophe or an em dash ("what’s", "harbor—crane") was stemmed whole under v1 and never met a page; it now splits exactly as the ASCII apostrophe or hyphen always did. This is intended.
- **The scanner costs more than the ASCII path.** A 6.5 KB English page with typographic dashes tokenizes in about 1.6× the ASCII path's time, because only the non-ASCII stretches are normalised and scanned.
- **No stemming for non-English Latin languages.** Accepted, as above.
- **A CJK turn earns no lexical `retrieval` in activation.** One run is one unit, and a single run never corroborates. That is conservative, and semantic evidence is its second contact.
- **The CJK carry stays off.** The carry reads rarity on the turn's surface forms and pairs two distinctive stems only from two different words, so an accented word and its folded variant never make a phrase with themselves, and the parts of a joined compound still pair. An unspaced run (Han, kana, Hangul, Thai and the like) contributes no pairable stem, so a turn in those scripts is never carried and runs no ranking query. This is a stated limit: two runs share particles and endings with every page in their script ("明日は、散歩です" carried a weather note on "日は" and "です"), the pair groups grow as the product of the two runs' bigrams (a 1,200-character Japanese turn took about a minute on a 1,600-page vault), and pairing them would need a position model over character offsets.
