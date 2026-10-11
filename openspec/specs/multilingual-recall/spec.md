# multilingual-recall Specification

## Purpose
Recall finds pages in every script and across languages: a script-keyed lexical tokenizer that needs no model, and one multilingual encoder for dense recall on a personal server, migrated blue/green without dense recall going dark.

## Requirements

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

### Requirement: A recall sidecar records its vector space and nothing mixes spaces

Every recall sidecar SHALL record the encoder that wrote it: the model, the encoder's fingerprint when a profiled encoder was resident (for a served model this names its revision, quantisation, file format and artefact digest), and the vector width. The width SHALL be read from that record wherever vectors are read, stored, mirrored into vec0 or reused, including the claims sidecar and warm-up. A sidecar with rows and no record SHALL be read as `BAAI/bge-base-en-v1.5` at 768 dimensions.

A write of another width SHALL be refused and leave the sidecar unchanged. A query SHALL be encoded for the sidecar the vector lane reads: the serving sidecar, or the build's sidecar while a build serves recall. It SHALL be encoded after that sidecar's encoder is checked against its record and before anything is encoded; when no encoder here serves that space, or a query vector encoded for one sidecar meets another, the vector lane SHALL be reported `unavailable` with reason `vector_space_mismatch`, and the lexical, keyword, graph and temporal lanes SHALL serve. Two fingerprints SHALL be compared exactly when both are known. The claims sidecar SHALL serve nothing from another space and SHALL re-encode every claim under the new one.

#### Scenario: A 1024-dimension sidecar round-trips

- **WHEN** bge-m3 vectors are written to an empty sidecar and read back by a new index
- **THEN** its record names bge-m3 at 1024 dimensions, and search, the matrix, vec0 and stored-vector reuse all read 1024-dimension rows

#### Scenario: Another build of the same model is refused

- **WHEN** the resident encoder is the sidecar's model but runs bytes with another artefact digest, and no build in the resident encoder's space serves recall
- **THEN** the vector lane is `unavailable` with `vector_space_mismatch`, the query is never encoded, and lexical recall answers

### Requirement: A new vector space is built beside the serving sidecar and cut over atomically

The serving recall sidecar SHALL be named by an active pointer beside it; with no pointer, `.embeddings.sqlite` serves. When another model wrote the serving sidecar, the serving sidecar SHALL keep serving on a personal server, with its queries and writes encoded by the encoder that wrote it. That encoder SHALL be loaded by warm-up before writes are admitted, or by the re-embed job, and never by a request; a query that finds it cold SHALL report the vector lane `warming`. A hosted or cloud cell SHALL hold one encoder: it SHALL NOT load the encoder that wrote the serving sidecar, a query SHALL never be encoded for that sidecar, its vector lane SHALL be reported `unavailable` with reason `vector_space_mismatch` while no build serves, and a write's encode for that sidecar SHALL fail soft and leave the page to the build.

A background job SHALL build a sidecar for the recall encoder's space beside the serving one, on a personal server and a cloud cell, in committed batches, through the chunking used by live writes, one text per encode, as bulk model work. It SHALL be resumable: a page is built when the new sidecar's rows carry its current mtime, and a restart SHALL NOT re-encode a built page. When the serving sidecar holds the build's own vector space, the build SHALL copy each vector that sidecar holds for the same text of the same page, and SHALL encode only the rest. While no active pointer is published, an initial build SHALL be due whenever the serving sidecar does not cover every eligible page and semantic unit, including when a live write has already given that sidecar the recall encoder's identity. A batch that writes no rows SHALL NOT fail the build. A page written during the build SHALL be encoded again before cutover. The cutover SHALL replace the active pointer in one atomic write, after catch-up passes, and SHALL take any write that landed in between; a failed cutover SHALL leave the old sidecar serving. The old sidecar SHALL be removed only by a later start of the job, never by the process that cut over, and only after one pass has caught the serving sidecar up with every page's current mtime. `EXOMEM_RECALL_REEMBED=off` SHALL build nothing: a personal server keeps the old sidecar serving with its own encoder, and a cell keeps its vector lane `unavailable` until the switch is lifted.

While a sidecar the job built in this process, completely or until it failed, is not named by the active pointer, the vector lane SHALL read it whenever the recall encoder cannot answer from the serving sidecar: during an initial build, on a hosted or cloud cell, and on a personal server whose serving sidecar another build of the same model wrote. The query SHALL then be encoded for the build's sidecar. The lane SHALL also read the serving sidecar when that sidecar records exactly the build's space (model, fingerprint and width), so that writes since the build began count. A page's rows in the build's sidecar SHALL answer only while they carry the page's current mtime: a page changed since the build encoded it SHALL answer from the serving sidecar alone, or from the lexical lanes, until a catch-up pass encodes it again. A page current in both sidecars SHALL keep its best score. Sidecars of different spaces SHALL never meet one query vector. Such a response SHALL add `embeddings` to `warming.components`, with `warming.since_s` null, and SHALL carry no other field about the build; a semantic-unit recall during the build SHALL carry the same mark. After the cutover the vector lane SHALL read only the serving sidecar and SHALL NOT mark recall warming. On a service cell, a query's encode SHALL wait for at most the one build passage in flight; on every install, a request SHALL take no model-slot turn to find a resident encoder.

Doctor SHALL report the serving space and a build's progress from the sidecars on disk. It SHALL report an initial build as in progress also when a live write has given the serving sidecar the build's space, but only while the job's rule would resume that build; a build sidecar the job will not resume SHALL be reported as left over, neither as a build in progress nor as a pass. It SHALL report a cell's refused sidecar as not serving, with dense recall limited to the pages built so far. The runtime status SHALL report a failed build as failed; a restart SHALL retry it. The runtime status SHALL report the running job's progress, rate and estimate. Progress SHALL appear only on these operator surfaces.

#### Scenario: Recall serves from the old sidecar until the cutover

- **WHEN** an installed vault's sidecar holds English vectors and a personal server upgrades
- **THEN** dense recall keeps answering from that sidecar with the English encoder while the bge-m3 sidecar builds, and answers from the bge-m3 sidecar after the cutover

#### Scenario: A cloud cell re-embeds with its one encoder

- **WHEN** a cloud cell's sidecar holds English vectors and the cell starts on bge-m3
- **THEN** the English encoder is never loaded, the vector lane is `unavailable` with `vector_space_mismatch` until the build starts and then reads the bge-m3 sidecar with recall marked warming, doctor warns that dense recall covers only the built pages, a page written meanwhile is in the bge-m3 sidecar after the cutover, and dense recall answers from the bge-m3 sidecar after the cutover without the mark

#### Scenario: A vault present before the first start is embedded without an operator

- **WHEN** a personal server or cloud cell starts over pages and semantic units already present in its vault, with no embedding sidecar or an empty one and no write receipts
- **THEN** its background job builds every eligible chunk and semantic unit in the recall encoder's space using committed batches, resumes without re-encoding committed work after interruption, catches up and atomically publishes the active pointer, and dense recall participates after cutover without an operator or a second encoder; during the build dense recall answers from the pages built so far with recall marked warming, and doctor reports the pending build or its progress rather than recommending CLI reconcile

#### Scenario: An initial build answers with its built pages and live writes

- **WHEN** an initial build has embedded part of the vault, a live write has given the serving sidecar the build's space, and no pointer names the build
- **THEN** a vector query answers from the build's sidecar and the serving sidecar together, a page only the build holds and the live-written page are both found, the response adds `embeddings` to `warming.components` and no other field, and after the cutover the same query answers without the mark

#### Scenario: A page changed after the build encoded it does not answer from its old rows

- **WHEN** the build has encoded a page, the page is rewritten and a live write puts its new text in the serving sidecar, and no catch-up pass has run
- **THEN** a vector query for the removed text does not return the removed text, and a query for the new text returns the page with its new text

#### Scenario: A personal server reads a build of its own model

- **WHEN** a personal server's serving sidecar was written by another build of the recall model and the job builds a sidecar with the resident build
- **THEN** the query is encoded for the build's sidecar, the vector lane reads it with recall marked warming, and the serving sidecar is never searched with that vector

#### Scenario: A failed build keeps serving what it built

- **WHEN** the build fails before its cutover
- **THEN** the vector lane keeps reading the pages it built, recall stays marked warming, the runtime status reports the build failed, and the next start retries it

#### Scenario: A leftover build sidecar is not a build in progress

- **WHEN** a build sidecar in the serving sidecar's space sits beside a serving sidecar that covers the vault, so the job will not resume the build
- **THEN** doctor reports the build sidecar as left over and not as a build in progress

#### Scenario: A query waits for at most one build passage

- **WHEN** a query arrives on a service cell while the build is encoding
- **THEN** its encode runs after the build passage in flight and before the build's next passage

#### Scenario: A live write does not orphan an interrupted initial build

- **WHEN** an initial build is interrupted, including before its first batch commits, and a live write has given the legacy sidecar the recall encoder's identity, with incomplete corpus coverage and no published active pointer
- **THEN** the next start resumes the initial shadow build without re-encoding committed batches, covers every eligible page and semantic unit, and reports current only after atomic cutover

#### Scenario: A save before the first plan does not hide an imported vault

- **WHEN** a server starts over an imported vault with no sidecar, and a live write reaches the legacy sidecar before the background job first plans
- **THEN** the job builds every eligible page and semantic unit, copies the live write's vectors instead of encoding them again, the response adds `embeddings` to `warming.components` until cutover, and the job publishes the active pointer

#### Scenario: A matching serving sidecar needs no startup coverage scan

- **WHEN** the serving sidecar accepts the recall encoder's identity and an active pointer is published
- **THEN** planning enumerates no pages and performs no per-page semantic coverage checks

#### Scenario: Drift in an unpublished sidecar encodes only what changed

- **WHEN** no active pointer is published and the legacy sidecar holds the recall encoder's space for every page except a few changed while the server was stopped
- **THEN** the job builds the space sidecar by copying the vectors the legacy sidecar holds for the unchanged pages and encoding only the changed pages, then publishes the active pointer

#### Scenario: A page that loses every unit does not stall the build

- **WHEN** a live write removes every semantic unit from a page the interrupted build already covered
- **THEN** the next start completes the build and publishes the active pointer

#### Scenario: Disabled or unavailable embeddings do not start a build

- **WHEN** embeddings are disabled or the optional serving stack is unavailable
- **THEN** the job reports a non-failure disabled or unavailable state with the existing serving-space details without loading an encoder or fetching its artifact; a served artifact requires ONNX Runtime and tokenizers even when Torch is the preferred backend

#### Scenario: An empty embedding corpus needs no encoder

- **WHEN** a vault contains no eligible chunk-bearing pages
- **THEN** the job is current without loading an encoder, repeated starts plan no build, and doctor emits no initial-build warning

#### Scenario: A failed cutover changes nothing

- **WHEN** writing the active pointer fails
- **THEN** the old sidecar keeps serving with its encoder, and the next cutover attempt succeeds from the built sidecar

#### Scenario: The kill switch holds the old space

- **WHEN** `EXOMEM_RECALL_REEMBED=off` is set on a personal server
- **THEN** no sidecar is built and the old sidecar keeps serving with its own encoder

#### Scenario: The kill switch on a cell keeps dense recall off

- **WHEN** `EXOMEM_RECALL_REEMBED=off` is set on a cell whose sidecar another model wrote
- **THEN** no sidecar is built, the vector lane stays `unavailable` with `vector_space_mismatch`, and doctor says the switch keeps dense recall off

### Requirement: Unspaced text is chunked under the encoder limit

A paragraph SHALL keep the 350-word cap. A paragraph that cap cannot bound (mostly unspaced text, more unspaced text than the cap allows, or a whitespace word longer than the character cap) SHALL be split into pieces of at most 500 characters: at a sentence end, else at the last space, else at the cap, and never between a base character and its combining marks. English chunk boundaries SHALL be unchanged, and no chunk SHALL exceed 512 bge-m3 tokens.

#### Scenario: A Japanese paragraph splits at its sentence ends

- **WHEN** a Japanese paragraph of 1,200 characters is chunked
- **THEN** every piece is at most 500 characters and ends at `。` where one falls under the cap

### Requirement: An other-script partial match does not outrank an invisible dense lead

When the vector lane's first candidate shares no content word with the query, a lexical lane SHALL withhold its vote for a page that matches the query only in part and whose dominant letter script differs from that candidate's. Such a page SHALL keep its dense and other votes. A page holding every content word of the query, or written in the lead's script, SHALL keep its lexical votes, and when the lead shares a content word with the query fusion SHALL be unchanged. The guard SHALL read only candidates that can reach the fused window, SHALL be timed as its own stage, and SHALL record each withheld vote and its reason in the explain trace and the lane status. Governed projected recall SHALL apply the same rule.

#### Scenario: A Russian partial match stays below the English gold

- **WHEN** a Russian query shares one word with a Russian page and nothing with the English page the dense lane ranks first
- **THEN** the English page ranks above the Russian page

#### Scenario: English ranking is unchanged

- **WHEN** the English golden set is ranked with and without the guard
- **THEN** no query's top 10 changes

### Requirement: Rerank is skipped where the reranker cannot read the query

Each declared reranker SHALL declare the letter scripts it reads and whether it reads across languages. A rerank SHALL be skipped, keeping the fused order, when the query's dominant script is outside the reranker's declared scripts, or when fusion reports that the query crosses into another language than the dense lead's and the reranker does not read across languages. An undeclared reranker SHALL NOT be gated. The skip SHALL be reported with its reason, and governed projected recall SHALL apply the same gate.

#### Scenario: A Chinese query over an English lead is not reranked by the base reranker

- **WHEN** a Chinese query shares no word with the English page the dense lane ranks first and the configured reranker is `BAAI/bge-reranker-base`
- **THEN** rerank is skipped with a crossing reason and the fused order is returned

#### Scenario: A German query over an English lead is still reranked

- **WHEN** a German query's dense lead is an English page
- **THEN** both are Latin script, no crossing is reported and the base reranker runs: the gate reads scripts, not languages

### Requirement: Dense multilingual acceptance is gated

The embeddings job SHALL gate the dense switch through `find`, hybrid, rerank off:

- the English golden fixture SHALL reach NDCG@10 of at least 0.897 and recall@10 of at least 0.9415, no query SHALL reach recall@10 of 0, and no query SHALL lose its best-graded page from the top 10;
- with the multilingual pages added, golden NDCG@10 SHALL stay within 0.02;
- per language, same-language queries SHALL reach recall@5 of at least 0.90, cross-language queries recall@10 of at least 0.80, and morphology queries (Russian, Estonian) recall@10 of at least 0.80;
- in the language-bias twins, the English gold SHALL be in the top 10 of every twin and SHALL outrank the same-language poison in most of a language's twins in at least three of the four languages, in every Russian and Japanese twin, and SHALL lose at most one rank to it in any twin;
- the Japanese-only vault SHALL reach hybrid recall@10 of at least 0.95;
- a short query SHALL encode within a p95 of 250 ms.

The typed-graph golden entries SHALL keep their graph-lane pins: the authored edge is indexed with its relation type, the graph lane expands it from the ideal page, and the neighbour enters fusion as a candidate. The fused rank of the one neighbour that ranks below the top 10 under bge-m3 SHALL NOT be asserted.

#### Scenario: A dense regression fails the gate

- **WHEN** an encoder or fusion change drops golden recall@10 below 0.9415
- **THEN** the embeddings job fails

### Requirement: A personal server and a cloud cell encode recall with one multilingual model

A personal server and a cloud cell SHALL encode recall with `BAAI/bge-m3`, served from its pinned ONNX Runtime int8 artefact on CPU, and activation SHALL share that one resident instance. A hosted cell SHALL keep `BAAI/bge-base-en-v1.5`; a malformed hosted-cell flag SHALL be read as a hosted cell. `EXOMEM_RECALL_MODEL` SHALL name another model explicitly on any of them. The hosted image SHALL carry the English model and the cloud image SHALL carry bge-m3, and each model's build SHALL fail unless it loads with the network closed and encodes at its declared width. The encoder SHALL read at most 512 tokens of a passage or query.

#### Scenario: A personal server loads one encoder

- **WHEN** a personal server has warmed recall and activation
- **THEN** one bge-m3 instance is resident and both encode with it

#### Scenario: A cloud cell encodes with the multilingual model

- **WHEN** `EXOMEM_CLOUD_CELL` is set and `EXOMEM_RECALL_MODEL` is not
- **THEN** recall encodes with `BAAI/bge-m3`

#### Scenario: A hosted cell keeps the English model

- **WHEN** `EXOMEM_HOSTED_CELL` is set to anything but a false value and `EXOMEM_RECALL_MODEL` is not
- **THEN** recall encodes with `BAAI/bge-base-en-v1.5`

#### Scenario: Each cell image carries the model its cells load

- **WHEN** the hosted and cloud images are built
- **THEN** each names the model its cells resolve, and the build fails unless that model loads offline and encodes at its declared width
