# Tasks

## 1. Contract and registries

- [ ] 1.1 Add the `brief` traversal profile to `traversal-profiles.yaml` and the `open_threads`/`open_thread` aliases to `open_question` in `kinds.yaml`.
- [ ] 1.2 Add the optional `brief` input to `read_memory` on every generated surface and `part="brief"` to `read_page`. Refuse incompatible options with `INVALID_VIEW_READ_OPTIONS`.

## 2. Brief

- [ ] 2.1 Resolve the seed from a page or topic text, with `related` hits and the not-found refusal.
- [ ] 2.2 Walk members from the seed through the `brief` profile and compiled notes only, attach cited Sources, and apply caps with `missing` counts.
- [ ] 2.3 Assemble `conclusions`, `sources` and `open_questions` from kind traits and the existing closing logic.
- [ ] 2.4 Cache the compiled brief under the input digest (D4). A warm read reads no page.
- [ ] 2.5 Serve `exomem://brief/<seed id>` as a derived view with pagination and continuation, and add the `brief` branch to the terminal egress guard.
- [ ] 2.6 Add the watermark sidecar and `changed_since` (D5).
- [ ] 2.7 Record brief-read latency and errors under their own route label. Teach the call in the scaffold operations reference.

## 3. Proof

- [ ] 3.1 Add tests for what users depend on:
  - a hub brief with its members and open questions;
  - a withheld member leaving the response unchanged;
  - cache invalidation and a warm read with zero page reads;
  - `changed_since` per principal;
  - `missing` and `related`.
- [ ] 3.2 Record cold and warm timings on a generated store at the load target, within the budgets.

## 4. Release and acceptance

- [ ] 4.1 Release, and upgrade the personal service through its normal procedure.
- [ ] 4.2 Prove a brief read from a real client against the personal store.
- [ ] 4.3 Run the evaluation (design, Evaluation) and record the per-case result.
