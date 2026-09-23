## ADDED Requirements

### Requirement: Lexical tokens read every script and stay identical on ASCII

The lexical tokenizer SHALL produce, for any ASCII text, exactly the tokens of tokenizer v1 (lowercase, `[a-z0-9]+`, English Snowball) on both the index side and the query side.

For other text it SHALL:

- treat every non-ASCII symbol (S*) and enclosing mark as a separator and drop variation selectors, before NFKC normalisation and then casefolding;
- take as a token a letter or number followed by any letters, numbers and combining marks, so a mark with no base never starts a token, with underscore and every punctuation mark separating;
- split a run where it crosses between a declared unspaced script (Han, kana, Hangul, Thai, Lao, Khmer, Myanmar) and a spaced one;
- emit overlapping two-character bigrams for an unspaced run, where a character is a base plus its combining marks, and the character itself for a one-character run.

The unspaced scripts SHALL be declared as Unicode block ranges in one module that every script-dependent rule reads. The tokenizer SHALL use no dictionary and no language detection.

#### Scenario: An English vault keeps its tokens

- **WHEN** a page or query is ASCII text
- **THEN** its index-side and query-side tokens equal tokenizer v1's tokens for the same text
- **AND** an English page whose only non-ASCII characters are punctuation, symbols, spaces or format characters tokenizes as it did under v1

#### Scenario: A symbol never joins the word beside it

- **WHEN** text says "Zorblex™", "20℃", "✔️ done" or "it´s"
- **THEN** it tokenizes as tokenizer v1 did: `zorblex`, `20`, `done`, and `it` plus `s`
- **AND** a number that NFKC turns into digits, such as "m²", stays in its word as `m2`

#### Scenario: Typographic punctuation in an English query splits as ASCII punctuation does

- **WHEN** a query word is "what’s", "don’t" or "harbor—crane"
- **THEN** find's stem gates read it as the words on either side of the punctuation, as they read "what's" and "harbor-crane"

#### Scenario: A Japanese page becomes searchable without a model

- **WHEN** embeddings are off and a Japanese page says "青葉タワーの高さは三百三十三メートルです"
- **THEN** its tokens include the bigrams of that run
- **AND** a Japanese query sharing a majority of those bigrams finds the page by lexical ranking

#### Scenario: Compatibility forms fold before splitting

- **WHEN** text contains full-width Latin letters or digits, or a Latin ligature
- **THEN** they tokenize as the ASCII letters and digits they stand for

### Requirement: A spaced word is stemmed by its script

A spaced word SHALL be stemmed by a pure function of the word: English Snowball for an ASCII word, Russian Snowball when all its letters are Cyrillic, Greek Snowball when all are Greek, Armenian Snowball when all are Armenian. Every other word, including a non-ASCII Latin word and a word mixing scripts, SHALL be left unstemmed. Query and index SHALL use the same function, so a word can never receive two different stems.

#### Scenario: A Russian inflection meets its page

- **WHEN** a page says "книги" and a query says "книгами"
- **THEN** both stem to the same token and the page matches

#### Scenario: A German inflection is matched only in its written form

- **WHEN** a page says "Prüfungen" and a query says "Prüfung"
- **THEN** the lexical lane does not treat them as one word

### Requirement: Accent-folded forms are indexed and queries use their surface

On the index side, when removing combining marks changes a word whose letters are all Latin, the tokenizer SHALL also emit the stem of the folded form. The query side SHALL emit surface forms only. The fold SHALL NOT apply to any other script.

#### Scenario: A query typed without accents finds the accented page

- **WHEN** a page says "Zölvarn" and a query says "zolvarn"
- **THEN** the page matches through its folded variant

#### Scenario: An accented query matches the exact surface only

- **WHEN** one page says "Zölvarn", another says "Zolvarn", and a query says "Zölvarn"
- **THEN** only the accented page matches
- **AND** no query term is counted twice

#### Scenario: Indic and Cyrillic words are never folded

- **WHEN** a page holds a Devanagari word with vowel signs or a Cyrillic word with й
- **THEN** only the word's own stem is indexed

### Requirement: Lexical corroboration counts words and runs, not stems

Tokenization SHALL expose the stems grouped by the word or unspaced run they came from. A corroboration rule that requires several distinct matches SHALL count distinct units: each stem belongs to the first query unit it came from, and a unit counts once. A word unit SHALL count when any of its stems occurs; an unspaced run SHALL count only when a strict majority of its content bigrams occur. On ASCII text this count SHALL equal the count of distinct stems.

#### Scenario: One Japanese sentence is one piece of evidence

- **WHEN** activation asks the catalogue for two corroborating matches and the turn is one unspaced run whose bigrams all occur on the page
- **THEN** the page is not corroborated
- **AND** two separate runs that both occur on the page corroborate it

#### Scenario: Shared particles are not corroboration

- **WHEN** the turn is "明日は、散歩です" and a page says "今日は晴れです"
- **THEN** the page is not corroborated, although it holds the bigrams 日は and です
- **AND** activation gives no anchor `retrieval` evidence for that turn

#### Scenario: One accented word is one piece of evidence

- **WHEN** the turn is the single word "Zölvarn" and the page holds both its surface and folded forms
- **THEN** the page is not corroborated

### Requirement: Find's word coverage treats an unspaced run as one word

Find's coverage gates SHALL treat each whitespace word as before and each unspaced run as one word, present when a strict majority of its content bigrams occur in the page. A run's content bigrams SHALL be its bigrams without hiragana and without the question kanji 何; when none remain, its bigrams without hiragana; and when every bigram holds hiragana, all of them. Rerank's query word count SHALL count ASCII whitespace words as before, and each unspaced run inside a non-ASCII word as a word.

#### Scenario: A Japanese question keeps its page with embeddings off

- **WHEN** the semantic lanes are absent and a Japanese question shares most of its bigrams, but not its question words, with a page
- **THEN** the page is retained

#### Scenario: A Japanese "X の Y" query finds its page

- **WHEN** the query is "抹茶の用意" and a page says "抹茶と和菓子は講師が用意します"
- **THEN** the page is retained, although the query's particle bigrams are not on it

#### Scenario: A question built on 何 finds its page

- **WHEN** the query is "パンは何度で焼きますか" and a page says "天然酵母のパンは一晩発酵させてから二百度で焼きます"
- **THEN** the page is retained: 何度 asks a question and is not required on the page

#### Scenario: A query of particles only finds nothing

- **WHEN** the query is "についてですか"
- **THEN** no page is retained on lexical evidence

#### Scenario: One shared bigram is not coverage

- **WHEN** a Japanese query shares one of its six bigrams with a page
- **THEN** the page is not retained on lexical evidence alone

### Requirement: The lexical catalogue stores tokens verbatim and rebuilds on upgrade

The catalogue's pre-stemmed FTS5 tables SHALL be declared so that FTS5 neither removes diacritics nor splits a token at a letter, number or mark, and every token the tokenizer emits SHALL be the token FTS5 stores. A token change SHALL move the catalogue schema version. A catalogue at an older version SHALL read not-current, be rebuilt by the existing background rebuild, and be re-declared by an in-place rebuild. Until then `find` SHALL answer `RETRIEVAL_INDEX_WARMING` for a vault it may not rebuild inline (more than 64 pages, or a managed runtime), and activation's lexical evidence SHALL report the catalogue as stale. A build SHALL hold an advisory lock on its temporary from before the temporary exists until it is cleaned up, and a rebuild SHALL begin by removing rebuild temporaries, with their WAL and SHM, whose lock no build holds, that are older than ten minutes, and that no connection holds open.

#### Scenario: FTS5 stores what the tokenizer emitted

- **WHEN** pages in several scripts are indexed
- **THEN** the FTS5 vocabulary equals the set of tokens stored in the catalogue's pre-stemmed text

#### Scenario: A catalogue built by tokenizer v1 is replaced

- **WHEN** a vault's catalogue was built at schema 10 with the default FTS5 tokenizer
- **THEN** a direct BM25 caller is served by the in-process rung, and `find` on a large vault answers `RETRIEVAL_INDEX_WARMING`, until the rebuild publishes
- **AND** the rebuilt catalogue carries the new declaration and finds accented and unspaced pages

#### Scenario: A killed rebuild's temp is reclaimed

- **WHEN** a rebuild temp and its WAL have been untouched for more than ten minutes, no build holds the temp's lock and no connection holds it open
- **THEN** the next rebuild removes them before it builds
- **AND** a fresh temp, one another connection holds open, and one whose build still holds its lock are kept, however old they look

### Requirement: Lexical multilingual acceptance is gated

The lexical half SHALL be gated, with embeddings off, on both lexical backends:

- the English golden fixture SHALL rank identically under tokenizer v1 and v2;
- with sixty non-English pages added to it, no golden relevant page SHALL move by more than one rank and mean NDCG@10 SHALL move by at most 0.01;
- in the multilingual golden set, same-language Japanese and Russian queries SHALL reach recall@10 of at least 0.80, and German and Estonian queries typed without diacritics at least 0.90;
- in an invented Japanese-only vault built through the product writers, every query SHALL yield a token, recall@10 SHALL be at least 0.85 and MRR at least 0.70, "X の Y" and question forms SHALL find their page, a query of particles only SHALL find nothing, and a Japanese substring SHALL find its page in keyword mode.

A floor SHALL be set at the measured value minus 0.08 and never below these bars.

#### Scenario: An English regression fails the gate

- **WHEN** a tokenizer change moves any golden relevant page by more than one rank in the padded tree
- **THEN** the acceptance test fails
