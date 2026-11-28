## Decisions

Use the existing read-beside admission and open one dedicated read-only connection on the caller's thread. Pass the pinned connection to row selection. Start the existing Records query budget after link preparation, at the same point as the ordinary route.

A store snapshot and a new query session each pin their own version. If an append commits between them, the query can pair a fresh row with an old source proof. A shared session removes that mismatch. The same session also keeps the connection's read-only main database and its bounded temporary work.

Open the snapshot when dispatch sees competing store work, the query covers an admitted collection, the release policy is empty, and current owner and connector proof permit unrestricted reading. An idle store serves the ordinary route. Restricted callers, typed queries, saved views, and derived collections also use that route. When query readers are full, the same route decides any refusal. Keep errors after an admitted query begins on its existing release path.

Release the writer checkout once the dedicated connection pins its snapshot. A sourced append can then take the writer exclusively while the query reads. End the selection transaction before the release filter rechecks the current contributors. Recheck writer authority and store capability before returning the page. Import progress keeps its existing pooled snapshot and checkout.

The Records load target calls for filtered pages at p95 within 300 ms at 5,000 rows and one second at 100,000 rows. Compare the same public query before and after at both sizes. Hold the store thread in a separate run to show that the query can still answer. Pause a query after it establishes its source proof, append a row, and verify that the query and its snapshot stay together.
