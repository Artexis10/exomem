# Design

## Context

Passage search encodes the query, scores every eligible declared-passage chunk in the embeddings sidecar by exact dense similarity, and verifies the top `limit` chunks against the published Source bytes. The decision here is to fuse a lexical ranking with that dense ranking. Page search already fuses lanes with reciprocal rank fusion (RRF), and the lexical store already owns a tokeniser, an FTS5 configuration and a query-term budget.

## Decisions

### Fuse with reciprocal rank fusion

Each ranking reaches 200 chunks deep, the largest `limit`, so a chunk that either ranking places inside any requested limit can vote. RRF sums `1/(k + rank)` over both rankings, with `k` from the active ranking configuration, which page search also uses. Equal scores share the better rank, so two identical passages always fuse to equal values. RRF needs no calibration between cosine similarity and BM25, whose scales differ by model and corpus. A weighted blend of raw scores would need a calibration for each embedding model, and the offline prototype reached its gain with untuned RRF.

### Fuse by default, and keep dense-only on request

A request with no `mode` or with `mode: "hybrid"` fuses. `mode: "vector"` runs today's exact dense path with the same depth, scores and backend. These two values are search protocol tokens, so any other mode is refused before retrieval with an error that names them. Fusing by default lets existing callers gain recall without a change, and `vector` remains for a caller that needs the old order.

### Bind lexical terms to the rows that dense ranking reads

The sidecar holds an FTS5 table of `bm25.tokenize` stems for each chunk row, keyed by the chunk row's rowid, with the lexical store's FTS5 tokeniser. Every sidecar connection that this release opens adds temporary triggers. They write or delete a row's terms in the statement that writes or deletes the chunk row, and they advance a coverage marker with the sidecar generation. Temporary triggers exist only on that connection, so an older release that opens the sidecar never meets a function it lacks.

A write without the triggers leaves the marker behind the generation. Lexical ranking then waits until a backfill catches up from the sidecar's path log, which derives again the terms of every path written since the marker. This also covers a rewritten row that reuses the old rowid. When the log does not reach back to the marker, the backfill rebuilds the table in batches and then catches up from the generation at which it began.

Lexical selection runs in one read transaction. It reads the generation token, checks coverage and returns chunk references under a chunk proof from that snapshot, as the dense selection does. Both rankings receive the same eligible set before ranking: declared Sources of the proved publication, intersected with scope, filters and relations, minus the paths whose vectors the store withholds. Only the fused top `limit` reaches byte verification. A chunk whose stored text differs from the published bytes is withheld and counted, and the selection is not refilled.

Each vault has its own sidecar, so its terms never meet another vault's rows. A retired or erased Source loses its chunk rows and their terms in the same statements. BM25 statistics span the whole sidecar. A passage request already requires whole-namespace content authority, so those statistics reveal nothing that the caller cannot read.

### Report the dense score at each rank

The score of the result at rank `r` is the dense similarity at dense rank `r`, and tied fused values share the score of their first rank. A fused reply therefore has the same score distribution as a dense-only reply at the same limit, and its first score equals the best dense similarity. A caller that maps scores with a stored map, or floors them, keeps the same meaning. A caller that refits its map from data refits on the same distribution. The score is not the passage's own cosine similarity. That value would let a caller that re-sorts by score undo the fusion. The raw RRF value would fall below any floor that was set on cosine scores. The reply adds `ranking: "fused"` or `ranking: "dense"`, and `recall_space_id` and `scoring_mode` keep their meaning.

### Prepare without slowing requests

A vault that never runs passage search builds no terms table. The first passage request on a sidecar of at most one backfill batch (2,000 chunk rows) builds the table inline. A larger sidecar builds it in one background thread, with a backoff after a failure, and a cell prepares it at warm-up under `service-v1`. Until coverage holds, the request ranks by dense similarity, reports `ranking: "dense"` and says so in its summary.

The query keeps the rarest 16 units through the lexical store's term budget and drops a unit that occurs on more than a tenth of the chunks. On 40,500 chunks of 40-word prose in 1,500 Sources, the lexical stage measured p95 from 10 to 26 ms in process for queries of 3 to 250 words. The full backfill took 1.3 s. Fusion of two 200-entry rankings costs under a millisecond, and the dense partial sort to 200 entries costs no more than one to `limit`. The passage latency bars at the MCP boundary stay unchanged, and the reference qualification must pass with fusion on.

## Risks and Trade-offs

- An older release that writes the sidecar suspends lexical ranking until the next backfill. Each such reply reports `ranking: "dense"`, so the downgrade is visible.
- The terms table grows the sidecar by about one and a half times its chunk text: 14 MB for 9.7 MB of text on the 40,500-chunk shape.
- A caller that read the passage score as the passage's own cosine similarity now receives the dense score of its rank. The values keep their distribution, and the `ranking` field marks the change.
