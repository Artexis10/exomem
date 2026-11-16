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
- [x] 2.7 Record brief-read latency and errors in metric series of their own. Teach the call in the scaffold operations reference.
- [x] 2.8 Keep an owner's member walk under the graph snapshot's token (D4). A warm owner read of an unchanged graph reads no edge.

## 3. Proof

- [x] 3.1 Add tests for what users depend on:
  - a hub brief with its members and open questions;
  - a withheld member leaving the response unchanged;
  - cache invalidation and a warm read with zero page reads;
  - `changed_since` per principal;
  - `missing` and `related`.
- [ ] 3.2 Record cold and warm timings on a generated store at the load target, within the budgets. The cold budget is not met on this store; see the last item.
  - Store: 20,401 pages in folders of 200, 180,401 graph nodes, 420,130 edges, a compiled note that links 10,000 pages from its body, and an entity page that 10,000 pages link. One request at a time on one machine in file mode, as the owner and as a caller that cannot read 10 percent of the pages. Cold reads clear the compiled-brief cache and the walk memo; steady warm reads are the second pass on. Another session's workers kept the machine at a load average of 9 to 12 on 20 cores during every run, so a cold p95 moves by up to a factor of two between runs.
  - Before (the commit before the bounded walk), owner: cold p50 391 to 1,400 ms and p95 1,414 to 1,529 ms (four shapes, n=19 to 40); warm p95 44 to 49 ms.
  - Before, restricted caller: each of the five shapes that reach the compiled hub made 10,642 to 10,708 release decisions and took 9.4 to 11.4 s, warm and cold alike (n=1 to 3 per shape). The page that does not reach it made 632 decisions.
  - After, owner, two runs on the final code: warm p95 37 to 53 ms (budget 250 ms). Cold p95 586 to 1,963 ms over eight shape-runs of n=19 to 40; three of the eight are above 1,000 ms (1,963, 1,055 and 1,079 ms). The same four shapes in two earlier runs of this branch gave 517 to 750 ms.
  - After, restricted caller, the same runs: warm p95 636 to 980 ms (budget 1,000 ms), slowest read 1,037 ms. Cold p95 1,000 to 1,433 ms over eight shape-runs.
  - Release decisions per restricted read, after: 313 to 887 one by one, in both runs, and 125 to 200 pages decided in batches, against 10,642 to 10,708 before.
  - The first read of a process with the compiled hub as the seed fills the policy, membership and link caches once: 9.6 and 11.2 s restricted (21.9 and 25.3 s before), 1.1 and 5.1 s as the owner (2.4 s before).
  - A restricted caller is not memoized (design, D4), so each warm read pays its decisions, about 0.6 ms each in file mode.
  - Open: the restricted cold p95 is above the 1,000 ms cold budget in every shape, by up to 43 percent, and the owner's is above it in three of eight shape-runs under this load. The budget is not relaxed. Store mode decides rows in batches and is not measured here.
  - Commands: the generated-store probe, run once per caller kind and code state with the workspace `src` or the pre-change `src` on `PYTHONPATH`. Logs: `probe19-before.log` and `probe20-before-restricted.log` (before), `probe22-after.log` and `probe23-after.log` (after), kept outside this repository.

## 4. Release and acceptance

- [ ] 4.1 Release, and upgrade the personal service through its normal procedure.
- [ ] 4.2 Prove a brief read from a real client against the personal store.
- [ ] 4.3 Run the evaluation (design, Evaluation) and record the per-case result.
