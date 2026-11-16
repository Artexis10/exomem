# Tasks

## 1. Contract and registries

- [x] 1.1 Add the `brief` traversal profile to `traversal-profiles.yaml` and the `open_threads`/`open_thread` aliases to `open_question` in `kinds.yaml`.
- [x] 1.2 Add the optional `brief` input to `read_memory` on every generated surface and `part="brief"` to `read_page`. Refuse incompatible options with `INVALID_VIEW_READ_OPTIONS`.

## 2. Brief

- [x] 2.1 Resolve the seed from a page or topic text, with `related` hits and the not-found refusal.
- [x] 2.2 Walk members from the seed through the `brief` profile and compiled notes only, attach cited Sources, and apply caps with `missing` counts.
- [x] 2.3 Assemble `conclusions`, `sources` and `open_questions` from kind traits and the existing closing logic.
- [x] 2.4 Cache the compiled brief under the input digest (D4). A warm read reads no page.
- [x] 2.5 Serve `exomem://brief/<seed id>` as a derived view with pagination and continuation, and add the `brief` branch to the terminal egress guard.
- [x] 2.6 Add the watermark sidecar and `changed_since` (D5).
- [x] 2.7 Record brief-read latency and errors under their own route label. Teach the call in the scaffold operations reference.
- [x] 2.8 Keep an owner's member walk under the graph snapshot's token (D4). A warm owner read of an unchanged graph reads no edge.

## 3. Proof

- [x] 3.1 Add tests for what users depend on:
  - a hub brief with its members and open questions;
  - a withheld member leaving the response unchanged;
  - cache invalidation and a warm read with zero page reads;
  - `changed_since` per principal;
  - `missing` and `related`.
- [ ] 3.2 Record cold and warm timings on a generated store at the load target, within the budgets.
  - Measured on a generated store of 20,401 pages, 180,401 graph nodes and 420,130 edges, with two 10,000-link hubs that are not compiled notes, as the owner, one request at a time on one machine. Cold reads clear the compiled-brief cache and the walk memo. Steady warm reads are the second pass on, after the first pass has filled both.
  - Without the walk memo (the commit before it), steady warm: ordinary page p50 213 ms and p95 568 ms (n=120); a page next to a hub p95 481 to 513 ms; a 10,000-link hub as the seed 870 to 936 ms (n=7).
  - With the walk memo, steady warm: ordinary page p50 33 ms and p95 38 ms (n=120); a page next to a hub p95 35 to 44 ms (n=60); a 10,000-link hub as the seed 30 to 31 ms (n=7). The slowest steady reads were 379 ms (ordinary page) and 310 ms (a page that links a hub). The warm budget holds for the owner.
  - Cold is unchanged by the memo. Ordinary page: p50 256 ms and p95 544 ms (n=40), within the budget. A 10,000-link hub as the seed: 788 ms and 914 ms (one read each); ten cold hub reads across five runs ranged from 788 to 1,087 ms, so that budget is at its limit.
  - Open: a restricted caller's walk is made on every read, and its warm read was not measured here. It costs what the no-memo numbers show for the walk. Keep this box open until a decision on that case or on the cold hub budget.

## 4. Release and acceptance

- [ ] 4.1 Release, and upgrade the personal service through its normal procedure.
- [ ] 4.2 Prove a brief read from a real client against the personal store.
- [ ] 4.3 Run the evaluation (design, Evaluation) and record the per-case result.
