## Context

The lexical lanes all read one tokenizer, `bm25.tokenize`: the FTS5 catalogue (`lexstore`), the in-process `rank_bm25` rung, find's stem gates and coverage, the unit lane, governed projected retrieval, referent attributes and activation's lexical evidence. It keeps `[a-z0-9]+` and English Snowball. The catalogue stores pre-stemmed text in `fts USING fts5(stemmed)`, whose default `unicode61` folds diacritics and splits at combining marks. That was harmless while tokens were ASCII, and would silently break the invariant that FTS and the in-process scorer see identical tokens once they are not.

## Goals / Non-Goals

**Goals:**

- Working keyword recall for every script, with embeddings off, on any install.
- Byte-identical tokens, index and scores for English text.
- No model and no new dependency in the lexical half.

**Non-Goals:**

- Language detection, dictionaries or word segmentation.
- Stemming for Latin languages other than English. German, Estonian and Finnish inflections match only in their exact form here; the dense half covers morphology, and a vault-declared Latin stemmer is a later lever.
- Activation's own index terms (`working_set_index.tokens_of`), which keep their rules.
- The dense half, which follows in this change.

## Decisions

### D1. Script-keyed tokens, no language detection

Text is NFKC-normalised and casefolded. For ASCII input that equals `.lower()`, so ASCII text keeps the v1 regex path and is untouched. Other text is split into maximal runs of Unicode letters, numbers and marks; underscore and all punctuation separate, as `[a-z0-9]+` always did. A run splits again where it crosses between an unspaced script and a spaced one.

An unspaced run emits overlapping bigrams of characters, where a character is a base plus its combining marks. A one-character run emits that character. A spaced word is stemmed by the script of its letters.

Why not detection: a query is short, so detecting its language is unreliable, and a query stem that differs from the index stem of the same word loses the match. Pages mix languages, and a detector's version drift would silently re-stem the index. Script-keyed stems cannot disagree between query and index, because they are the same function of the same string.

The unspaced-script table is declared Unicode block ranges in `text_scripts.py`. It is data about scripts, not a word list, and any later rule that needs to know whether text is unspaced (CJK containment in activation, chunk caps for unspaced paragraphs) reads the same table.

### D2. Accent fold on the index side only

If removing combining marks (NFD, drop `Mn`, NFC) changes a word whose letters are all Latin, the index also stores the folded form's stem. "Zölvarn" stores `zölvarn` and `zolvarn`; "résumé" stores `résumé` and `resum`.

The query side emits surface forms only. A query typed without accents matches the folded variant, and a query typed with accents matches the exact surface. No query term is counted twice, and an accented query never widens to unaccented pages.

The fold is restricted to Latin because dropping marks destroys Indic words ("हिन्दी" would become "हिनदी") and conflates Cyrillic й with и.

### D3. Units, not stems, for corroboration

`token_units` returns the stems grouped by the word or run they came from. Activation's lexical evidence requires two corroborating matches. Counted over stems, one accented word (surface plus fold) or one Japanese sentence (many bigrams) would be its own second vote. The catalogue's corroboration filter therefore maps every stem to the first query unit it came from and counts distinct units. On ASCII every unit is one stem, so the count equals v1's count of distinct stems.

### D4. Find's word coverage treats an unspaced run as one word

Find's degraded-retention gate and its all-words veto count whole query words. A whitespace word keeps its v1 rule: present when every subtoken stem is present, so exact-marker compounds stay precise. An unspaced run is its own word, present when a strict majority of its distinct bigrams occur in the page.

- Requiring every bigram would demand the query's exact phrasing, particles included: "会議の議事録はいつ共有" would miss a page that says "議事録は翌日までに共有します".
- Accepting any one bigram would retain a page that shares a single particle bigram.

Rerank's word count counts whitespace words as before, and each unspaced run inside a non-ASCII word as a word.

### D5. Catalogue declaration and rebuild

`fts` and `unit_fts` are declared `tokenize="unicode61 remove_diacritics 0 categories 'L* N* Co M*'"`. Probed on SQLite 3.53.1, every NFKC-casefolded letter, number and mark round-trips as its own token (0 mismatches over 140,675 code points).

`SCHEMA_VERSION` moves from 10 to 11. A v10 catalogue reads not-current, so recall serves the in-process rung and activation reports `stale` until the existing atomic background rebuild publishes. The in-place rebuild path re-declares FTS tables whose declaration predates v2; `CREATE ... IF NOT EXISTS` alone would refill them under the old tokenizer.

### D6. English non-regression is measured, not assumed

- A property test compares v2 with a frozen copy of v1 over 20,000 random ASCII strings, in both modes, and across the fast-path/scanner boundary.
- The golden fixture's pages tokenize identically under v1 and v2, and its golden queries rank identically on both lexical backends.
- With sixty German, Russian, Japanese and Estonian pages added, no golden page moves by more than one rank and NDCG@10 moves by at most 0.01.

LongMemEval is not part of this gate.

## Risks / Trade-offs

- **Postings grow for unspaced text.** One token per character against about one per five for English, so a Japanese vault's catalogue grows about 5× per byte of text. FTS5 handles it; the rebuild time on a 3,000-page fixture is recorded in tasks.md.
- **Corpus statistics move in mixed vaults.** Only pages with non-ASCII letters change their tokens, which moves average length and document frequency. This is bounded by the padded golden arm.
- **No stemming for non-English Latin languages.** Accepted, as above.
- **A CJK turn earns no lexical `retrieval` in activation.** One run is one unit. That is conservative, and semantic evidence is its second contact.
- **Activation's carry pairs stems.** The carry pairing in the batch-1 activation work places every stem of one raw token at one position. It needs the unit rule too, so the merge is gated on that rule being on the base.
