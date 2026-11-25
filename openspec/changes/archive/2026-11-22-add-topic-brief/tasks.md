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
- [x] 2.9 Keep at most five second-hop notes, nearest the seed first, and report `ranked_by` (D2). Key the owner's walk memo on the token of the scorer that ranked it. Tests: the second hop keeps the nearest notes and counts the rest; with no vector reading it keeps the newest and says so; a note embedded after a read takes its place on the next read; a kept walk follows the matrix that ranked it through a lag on one page, a lag on another and a cold matrix; a profile with no resident matrix still reads no edge on a warm read; a restricted caller's brief of a seed whose only neighbour is withheld is the brief of a store without that page, with and without vectors.
  - Latency, 2026-11-17: the 3.2 store rebuilt (20,402 pages, 180,404 nodes, 423,133 edges, file mode) with one planted recall vector per page and the matrix resident, so the nearness reading runs (`ranked_by: "similarity"` in every shape). The code before this task and after it ran back to back on the same store, at a load average of 2.5 to 3.8, with the 3.2 shapes and callers.
  - Owner: warm p95 46 to 55 ms before, 40 to 59 ms after (budget 250 ms); cold p95 275 to 803 ms before, 291 to 634 ms after (budget 1,000 ms).
  - Restricted caller: cold p95 755 to 1,233 ms before, 640 to 928 ms after; warm p95 941 to 1,297 ms before, 527 to 1,070 ms after (budget 1,000 ms for both). The one shape still above it is the hub with 1,495 of 1,500 spokes withheld, warm 1,070 ms (n=7). One-by-one release decisions per read: 313 to 1,771 before, 169 to 1,619 after, because fewer second-hop pages are inspected.
  - Logs: `probe-before2.log` and `probe-after2.log`, kept outside this repository. An earlier pair ran at unequal load and is not used.

## 3. Proof

- [x] 3.1 Add tests for what users depend on:
  - a hub brief with its members and open questions;
  - a withheld member leaving the response unchanged;
  - cache invalidation and a warm read with zero page reads;
  - `changed_since` per principal;
  - `missing` and `related`.
  - a hub that cites more Sources than a step reads still serves every linked note, and `missing` carries no count of 0 beside `exact: false`.
- [x] 3.2 Record cold and warm timings on a generated store at the load target against the budgets. The owner's budgets hold. The restricted caller's budgets are missed in file mode, and the owner accepted that miss on 2026-11-16 until the store switch; see the last item.
  - Store: 20,402 pages in folders of 200, 180,404 graph nodes, 423,133 edges, in file mode. It holds a compiled note that links 10,000 pages from its body, an entity page that 10,000 pages link, and a compiled hub with 1,500 spokes of which 1,495 are withheld from the restricted caller. One request at a time on one machine, as the owner and as a caller that cannot read 10 percent of the pages. Cold reads clear the compiled-brief cache and the walk memo; steady warm reads are the second pass on. Another session's workers kept the machine at a load average of 12 to 13 on 20 cores for both runs, so a p95 moves by up to a factor of two between runs.
  - Before the bounded walk, restricted caller: each of the five shapes that reach the compiled hub made 10,642 to 10,708 release decisions and took 9.4 to 11.4 s. Owner cold p95 was 1,414 to 1,529 ms.
  - The two runs below read the same store, one after the other: the code before the per-role row bound (`probe-r1-before.log`) and the final code (`probe-r1-after.log`). Shapes of n=19 to 120 give the p95 ranges.
  - Owner, warm p95: 52 to 61 ms before, 56 to 63 ms after (budget 250 ms). Cold p95: 585 to 627 ms before, 764 to 864 ms after (budget 1,000 ms); the slowest cold read is 932 ms.
  - Restricted caller, warm p95: 843 to 1,062 ms before, 985 to 1,218 ms after (budget 1,000 ms); slowest read in those shapes 1,369 ms. Cold p95: 1,111 to 1,198 ms before, 1,174 to 1,397 ms after.
  - The per-role row bound costs one indexed read per role when a page links more rows than its share, and that read tests the far page of each row. On a page with 10,000 links it takes about 100 ms per role against 6 to 9 ms for the untyped read. A page that fits its share keeps the single untyped read, after a probe that stops at one row more than the share. The extra cold time is on the shapes that reach a hub.
  - Release decisions per restricted read, outside the withheld-hub shape: unchanged by the bound, 313 to 888 one by one and 125 to 200 pages decided in batches, against 10,642 to 10,708 before the bounded walk.
  - The compiled hub with 1,500 spokes, 1,495 withheld: 1,771 one-by-one decisions and 90 batched, before and after. Restricted cold 1,448 ms before and 1,246 ms after (n=1); warm p95 1,728 ms before and 1,657 ms after (n=7). Owner warm p95 54 and 60 ms.
  - A restricted caller is not memoized (design, D4), so each warm read pays its decisions, about 0.6 ms each in file mode.
  - Open: restricted cold p95 is above the 1,000 ms cold budget in every shape, restricted warm p95 is above its 1,000 ms budget in three of the four larger shapes after (two before) under this load, and the hub with withheld spokes is the slowest at 1.2 to 1.7 s. The budget is not relaxed, and no scan cap counts withheld rows. Store mode decides rows in batches and is not measured here.
  - Decision (owner, 2026-11-16): ship with the restricted caller's miss recorded. It holds until the switch to store mode (`make-vault-store-canonical`). The one-by-one release decisions belong to the shared graph reader's file-mode path, which predates this change, and are tracked as debt there. Production records the brief's latency in `exomem_brief_read_duration_ms`.
  - Commands: the generated-store probe through the request dispatcher, run once per code state with the workspace `src` or the pre-change `src` on `PYTHONPATH`. Logs: `probe-r1-before.log` and `probe-r1-after.log`, with `uptime` before and after each run, kept outside this repository.

## 4. Release and acceptance

- [x] 4.1 Release, and upgrade the personal service through its normal procedure.
  - v0.189.0 is the first release with the second-hop member rule (2.9). The personal service was upgraded through its normal procedure and runs a later release.
- [x] 4.2 Prove a brief read from a real client against the personal store.
  - 2026-11-22, Claude Code over MCP against the personal store. A researched subject's brief, read to its last page, returned `ranked_by: "similarity"` and 8 compiled notes: the seed, 2 notes one hop away and 5 notes two hops away. It counted 38 more under `cap`, with an exact count. `changed_since` said `first_read`; a second read said `unchanged` at the same generation.
  - Against that subject's independent answer key, the brief held both core members and 2 of the 4 peripheral members, and `related` named the other 2. Four of the 8 notes were outside the key.
- [x] 4.3 Run the evaluation (design, Evaluation) and record the per-case result. Development round only; the decision run moved to `route-subject-returns-to-brief` (see the decision below).
  - 2026-11-22 development round: five researched subjects, one draw per arm, the shipped Claude Code client on the release the personal service runs, a blind Claude judge against independent keys. The per-case results are kept in the personal eval-run record, outside this repository.
  - The routing line in the client skill never reached the model: no run in any arm opened the skill, and the model followed the server's instructions instead.
  - With the line given directly, every brief call answered `index_warming` with no items, because the service's graph read snapshot was waiting on its background cold proof. The recap scores therefore measured failed brief calls, not the brief: composite difference -0.055, 95% interval -0.117 to +0.006, five subjects.
  - Decision (owner, 2026-11-22): close this change with the brief shipped, and move the routing decision to `route-subject-returns-to-brief`, which routes through the server's instructions and is evaluated once the brief serves.
