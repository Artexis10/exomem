# Design

## Context

Passage search encodes the query, scores every eligible declared-passage chunk in the embeddings sidecar by exact dense similarity, and verifies the top `limit` chunks against the published Source bytes. The decision here is to let a caller fuse a lexical ranking with that dense ranking. Page search already fuses lanes with reciprocal rank fusion (RRF), and the lexical store already owns a tokeniser, an FTS5 configuration and a query-term budget.

## Decisions

### Fuse with reciprocal rank fusion

Each ranking reaches 200 chunks deep, the largest `limit`, so a chunk that either ranking places inside any requested limit can vote. RRF sums `1/(k + rank)` over both rankings, with `k` from the active ranking configuration, which page search also uses. Equal scores share the better rank, so two identical passages always fuse to equal values. RRF needs no calibration between cosine similarity and BM25, whose scales differ by model and corpus. A weighted blend of raw scores would need a calibration for each embedding model, and the offline prototype reached its gain with untuned RRF.

### Keep dense ranking by default, and fuse on request

A request with no `mode` or with `mode: "vector"` runs today's exact dense path with the same depth, scores and backend. `mode: "hybrid"` fuses. These two values are search protocol tokens, so any other mode is refused before retrieval with an error that names them. The offline prototype is not a decision run: its cases were also used to build it. The default changes only after a held-out decision run shows that fused ranking is non-inferior or better, and that change keeps `mode: "vector"` for a caller that needs the old order. Page search keeps its own `hybrid` default, so the leaf receives an omitted mode as omitted, and each result level applies its own default.

### Bind lexical terms to the rows that dense ranking reads

The sidecar holds an FTS5 table of `bm25.tokenize` stems for each chunk row, keyed by the chunk row's rowid, with the lexical store's FTS5 tokeniser. A names table holds each row's path and chunk number under the same rowid, and `meta` holds the number of chunk rows. Every sidecar connection that this release opens adds temporary triggers. They write or delete a row's terms and name in the statement that writes or deletes the chunk row, keep the row count, and advance a coverage marker with the sidecar generation. Temporary triggers exist only on that connection, so an older release that opens the sidecar never meets a function it lacks.

A write without the triggers leaves the marker behind the generation. Lexical ranking then waits until a backfill catches up from the sidecar's path log, which derives again the terms of every path written since the marker. This also covers a rewritten row that reuses the old rowid. When the log does not reach back to the marker, the backfill rebuilds the tables in batches and then catches up from the generation at which it began.

The first release kept the terms without names, under its own coverage marker. This release keeps a marker of its own, so a sidecar from that release, or one that it wrote after a rollback, is rebuilt or caught up before its names are trusted. A rebuild also deletes the earlier marker, so that release never trusts a half-rebuilt terms table after a rollback.

Lexical selection runs in the dense selection's read snapshot. A `hybrid` request opens one read-only connection and reads the generation token once. The resident matrix serves the request only when it matches that token, and the lexical stage checks coverage and returns chunk references under a chunk proof of the same token. When the matrix does not serve, the lexical stage runs inside the disk scorer's snapshot instead. Both rankings therefore carry one proof, and byte verification reads the sidecar once for both. Only when coverage is missing does the request open another connection, to decide whether it may make the terms whole. Both rankings receive the same eligible set before ranking: declared Sources of the proved publication, intersected with scope, filters and relations, minus the paths whose vectors the store withholds. Only the fused top `limit` reaches byte verification. A chunk whose stored text differs from the published bytes is withheld and counted, and the selection is not refilled.

Each vault has its own sidecar, so its terms never meet another vault's rows. A retired or erased Source loses its chunk rows and their terms in the same statements. BM25 statistics span the whole sidecar. A passage request already requires whole-namespace content authority, so those statistics reveal nothing that the caller cannot read.

### Report the dense score at each rank, and each hit's own similarity

The `score` of the result at rank `r` is the dense similarity at dense rank `r` of the same request, and tied fused values share the score of their first rank. It is a value of the rank, not of the passage. A fused reply therefore has the same score distribution as a dense-only reply at the same limit, and its first score equals the best dense similarity. A score map or floor fitted to dense scores applies to it as a map of rank positions. A caller that refits its map from data refits on the same distribution. The raw RRF value would fall below any floor that was set on cosine scores, and the passage's own similarity in `score` would let a caller that re-sorts by score undo the fusion.

Each result also carries `similarity`, its own dense similarity to the query. It is null for a lexical match outside the dense ranking's top 200 chunks, because the request computed no similarity for it, and a zero would claim a value. In a dense-only reply, `similarity` equals `score`. The reply adds `ranking: "fused"` or `ranking: "dense"`, and `recall_space_id` and `scoring_mode` keep their meaning.

### Prepare without slowing requests

A vault that never runs a `hybrid` passage search builds no terms table, unless its cell prepares one at warm-up under `service-v1`. The first `hybrid` request on a sidecar of at most one backfill batch (2,000 chunk rows) builds the table inline. A larger sidecar builds it in one background thread. After a failed backfill, neither path starts another until the retry delay has passed, because each attempt rescans every row. Until coverage holds, a `hybrid` request ranks by dense similarity and reports `ranking: "dense"`. It reports the lexical state as `diagnostics.lexical`: `ready`, `preparing`, or `failed` after a failed backfill. The summary is the outcome line that an agent repeats to the user, so it does not mention indexing state.

A rebuild of the whole sidecar replaces every chunk row in one publication transaction. The terms triggers would derive every row's stems inside that transaction, and at about 200,000 chunks the write lock then outlasts the 5-second busy timeout of other writers. The rebuild therefore drops its own connection's triggers and deletes the coverage marker in that transaction. The background backfill then rebuilds the table in committed batches, and `hybrid` requests rank by dense similarity until it finishes.

### Rank lexical candidates from the terms index alone

The lexical stage is on the request path of every `hybrid` call, so its cost counts against the passage latency bars. Three choices keep it small.

The terms table alone orders the matches by BM25, and the names table names them, one primary-key read per batch of `limit` matches. The stage keeps the eligible matches in rank order until it holds `limit` rows. A join or a lookup of each match's chunk row reads one leaf page of the chunks table per candidate. A search reads a few hundred of them, spread over the 21 MB of leaf pages on the 40,500-chunk reference. In the 3 GiB reference container, the file cache reaches the memory limit, and the kernel reads evicted pages again. One hybrid segment then read 209 MB from disk in its single-caller window, and its mean tool time was 282 ms. Another, which read 4.5 MB, had a mean of 133 ms.

An earlier shape kept the names in process memory, as a map that the first search after each chunk write rebuilt from every row. That rebuild grew with the sidecar:

| Chunks | Map rebuild after a write | Map held | Names table on disk | Naming 200 matches |
| ---: | ---: | ---: | ---: | ---: |
| 40,500 | 14 ms | 5.0 MiB | 2.5 MB | 0.08 ms |
| 200,000 | 71 ms | 29 MiB | 12.3 MB | 0.09 ms |
| 1,000,000 | 428 ms | 135 MiB | 61.3 MB | 0.09 ms |

The map figures come from the review of the first shape. The names-table figures come from a synthetic table with 48-character paths on a developer host, with a warm file cache, best of 20 reads. The names table costs no rebuild after a write, holds no process memory, and its pages are cached like the rest of the sidecar.

BM25 term selection also needs the row count and each query stem's count of rows. The triggers keep the row count in `meta`, so a search reads it in its snapshot. The index keeps each stem's count for one sidecar token: the epoch, generation and instance of the chunk proof. Each stem's count is read once per token, on the first search that needs it, and counts from an older token are replaced and never edited. The index keeps no query results, rankings or embeddings across requests.

The resident path reads the sidecar on a read-only connection, as the chunk inventory does, without the write ceremony of a sidecar connection. It serves no text: the fused top `limit` is verified against the published bytes as before. A `hybrid` reply reports the number of lexical candidates as `diagnostics.lexical_candidates`, so a reply whose lexical ranking was empty, and whose order is therefore the dense order, is distinguishable from one that fusion changed.

### Keep the rarest distinctive units

The MATCH reads every row of each stem that it asks for, so the rows of common units set its cost. From 500 chunks, a query unit on more than 3% of the chunks is common and is dropped. The query keeps the rarest 6 remaining units. A query of only common units gets no lexical candidates, and its fused ranking equals its dense ranking. Below 500 chunks the MATCH is cheap whatever it reads, so no unit is common there.

The budget was chosen by measurement on 49 real chat questions over 33,454 chunks, with key coverage counted in the fused top 5, 15 and 30. Those questions also scored it, so its gains are in-sample until the held-out decision run of task 3.3. The numbers live in the ranking configuration beside `rrf_k`. On the 40,500-chunk reference, the lexical stage was timed in process for the 40 reference queries, with all counts read.

| Budget | Top 5 | Top 15 | Top 30 | Lexical stage median / p95 |
| --- | ---: | ---: | ---: | ---: |
| Dense ranking alone | 6 | 13 | 21 | none |
| Previous: rarest 16 under 10%, else the rarest common units | 11 | 20 | 27 | 5.7 / 14.6 ms |
| Rarest 6 under 2% | 6 | 19 | 24 | 1.9 / 3.3 ms |
| Rarest 6 under 3% (chosen) | 9 | 21 | 25 | 2.2 / 3.1 to 4.1 ms |
| Rarest 16 under 3% | 10 | 20 | not run | 2.7 / 6.2 ms |
| Rarest 16 under 10%, no common units kept | 11 | not run | not run | 5.2 / 12.7 ms |

The chosen budget keeps the top-15 gain and most of the top-5 gain. It gives up 2 cases at top 30. Keeping the rarest common units when no other unit remained cost a p95 of 14.3 ms under 2%, because 5 of the 40 reference queries have no unit under that share. The previous budget measured on the same code, so its cost is the cost of the rows it reads.

Fusion of two 200-entry rankings costs under a millisecond, and the dense partial sort to 200 entries costs no more than one to `limit`. The passage latency bars at the MCP boundary stay unchanged, and the reference qualification must pass with fusion on. In process on the reference, a `hybrid` search now opens the two connections and reads the three tokens of a dense search, where it opened three and read four. With four callers, its lexical stage took a median of 2.2 ms, where it took 6.2 ms when it read its own snapshot. On the first search after a chunk write, it took a median of 3.0 ms, where the stage that rebuilt the in-memory names took 28.6 ms. The 22 November qualification met the single-caller bar with fusion on and missed the four-caller bar by 24.9 ms. A second run, with the lexical stage in the dense snapshot, met both bars on a loaded host; task 3.2 records the numbers.

## Risks and Trade-offs

- An older release that writes the sidecar suspends lexical ranking until the next backfill. Each such `hybrid` reply reports `ranking: "dense"`, so the downgrade is visible.
- A whole-index rebuild suspends lexical ranking until the backfill has derived the new rows' terms.
- The terms table grows the sidecar by about one and a half times its chunk text: 14 MB for 9.7 MB of text on the 40,500-chunk shape. The names table adds about 61 bytes per chunk row for a 48-character path.
- A caller of a fused reply that reads `score` as the passage's own cosine similarity reads the dense score of its rank. The `ranking` field marks a fused reply, and `similarity` holds the passage's own value.
