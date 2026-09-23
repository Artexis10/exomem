## ADDED Requirements

### Requirement: Lexical tokens read every script and stay identical on ASCII

The lexical tokenizer SHALL produce, for any ASCII text, exactly the tokens of tokenizer v1 (lowercase, `[a-z0-9]+`, English Snowball) on both the index side and the query side.

For other text it SHALL:

- apply NFKC normalisation and then casefolding;
- take maximal runs of Unicode letters, numbers and combining marks as tokens, with underscore and every punctuation mark separating;
- split a run where it crosses between a declared unspaced script (Han, kana, Hangul, Thai, Lao, Khmer, Myanmar) and a spaced one;
- emit overlapping two-character bigrams for an unspaced run, where a character is a base plus its combining marks, and the character itself for a one-character run.

The unspaced scripts SHALL be declared as Unicode block ranges in one module that every script-dependent rule reads. The tokenizer SHALL use no dictionary and no language detection.

#### Scenario: An English vault keeps its tokens

- **WHEN** a page or query is ASCII text
- **THEN** its index-side and query-side tokens equal tokenizer v1's tokens for the same text
- **AND** an English page whose only non-ASCII characters are punctuation tokenizes as it did under v1

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

Tokenization SHALL expose the stems grouped by the word or unspaced run they came from. A corroboration rule that requires several distinct matches SHALL count distinct units: each stem belongs to the first query unit it came from, and a unit counts once however many of its stems occur. On ASCII text this count SHALL equal the count of distinct stems.

#### Scenario: One Japanese sentence is one piece of evidence

- **WHEN** activation asks the catalogue for two corroborating matches and the turn is one unspaced run whose bigrams all occur on the page
- **THEN** the page is not corroborated
- **AND** two separate runs that both occur on the page corroborate it

#### Scenario: One accented word is one piece of evidence

- **WHEN** the turn is the single word "Zölvarn" and the page holds both its surface and folded forms
- **THEN** the page is not corroborated

### Requirement: Find's word coverage treats an unspaced run as one word

Find's coverage gates SHALL treat each whitespace word as before and each unspaced run as one word, present when a strict majority of its distinct bigrams occur in the page. Rerank's query word count SHALL count ASCII whitespace words as before, and each unspaced run inside a non-ASCII word as a word.

#### Scenario: A Japanese question keeps its page with embeddings off

- **WHEN** the semantic lanes are absent and a Japanese question shares most of its bigrams, but not its question words, with a page
- **THEN** the page is retained

#### Scenario: One shared bigram is not coverage

- **WHEN** a Japanese query shares one of its six bigrams with a page
- **THEN** the page is not retained on lexical evidence alone

### Requirement: The lexical catalogue stores tokens verbatim and rebuilds on upgrade

The catalogue's pre-stemmed FTS5 tables SHALL be declared so that FTS5 neither removes diacritics nor splits a token at a letter, number or mark, and every token the tokenizer emits SHALL be the token FTS5 stores. A token change SHALL move the catalogue schema version. A catalogue at an older version SHALL read not-current, be rebuilt by the existing background rebuild, and be re-declared by an in-place rebuild. Until then recall SHALL serve the in-process rung with the current tokenizer, and activation's lexical evidence SHALL report the catalogue as stale.

#### Scenario: FTS5 stores what the tokenizer emitted

- **WHEN** pages in several scripts are indexed
- **THEN** the FTS5 vocabulary equals the set of tokens stored in the catalogue's pre-stemmed text

#### Scenario: A catalogue built by tokenizer v1 is replaced

- **WHEN** a vault's catalogue was built at schema 10 with the default FTS5 tokenizer
- **THEN** recall is served by the in-process rung until the rebuild publishes
- **AND** the rebuilt catalogue carries the new declaration and finds accented and unspaced pages

### Requirement: Lexical multilingual acceptance is gated

The lexical half SHALL be gated, with embeddings off, on both lexical backends:

- the English golden fixture SHALL rank identically under tokenizer v1 and v2;
- with sixty non-English pages added to it, no golden relevant page SHALL move by more than one rank and mean NDCG@10 SHALL move by at most 0.01;
- in the multilingual golden set, same-language Japanese and Russian queries SHALL reach recall@10 of at least 0.80, and German and Estonian queries typed without diacritics at least 0.90;
- in an invented Japanese-only vault built through the product writers, every query SHALL yield a token, recall@10 SHALL be at least 0.85 and MRR at least 0.70, and a Japanese substring SHALL find its page in keyword mode.

A floor SHALL be set at the measured value minus 0.08 and never below these bars.

#### Scenario: An English regression fails the gate

- **WHEN** a tokenizer change moves any golden relevant page by more than one rank in the padded tree
- **THEN** the acceptance test fails
