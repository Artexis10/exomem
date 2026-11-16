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

## 3. Proof

- [x] 3.1 Add tests for what users depend on:
  - a hub brief with its members and open questions;
  - a withheld member leaving the response unchanged;
  - cache invalidation and a warm read with zero page reads;
  - `changed_since` per principal;
  - `missing` and `related`.
- [ ] 3.2 Record cold and warm timings on a generated store at the load target, within the budgets.
  - Measured on a generated store of 20,401 pages, 180,401 graph nodes and 420,130 edges, with two 10,000-link hubs that are not compiled notes, one request at a time on one machine.
  - An ordinary page: cold p50 372 ms and p95 623 ms (n=40); warm p50 226 ms and p95 652 ms (n=120). A page next to a hub: cold p95 612 ms, warm p95 552 ms.
  - A 10,000-link hub as the seed: cold 882 to 1,086 ms (one read per hub), warm 953 to 966 ms (three reads). A compiled-note hub inside the walk raises the p95 of both to 1.4 to 1.6 s.
  - The cold budget holds for an ordinary page. The warm p95 budget and the hub-as-seed budgets do not hold: the walk reads about 21,000 edge rows of a hub through the shared graph view, and each page it reaches is interpreted again per request. Keep this box open until a graph-index change, or a decision on the budget, closes the gap.

## 4. Release and acceptance

- [ ] 4.1 Release, and upgrade the personal service through its normal procedure.
- [ ] 4.2 Prove a brief read from a real client against the personal store.
- [ ] 4.3 Run the evaluation (design, Evaluation) and record the per-case result.
