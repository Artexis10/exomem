## 1. Restore the complete product surface in hosted cells

- [x] 1.1 Add a failing rendered-manifest test asserting the cell environment does not contain `EXOMEM_DISABLE_EMBEDDINGS` or `EXOMEM_DISABLE_MEDIA_EXTRACTION`, and that the worker limit is greater than zero with the `embeddings` grant present.
  - Media extraction is out of scope for the alpha; the assertion covers embeddings and the file watcher. See 1.5.
- [x] 1.2 Set the cell chart's worker limit and feature grants accordingly, and make rendering fail closed when a zero worker limit would silently produce keyword-only recall.
  - Two layers: `values.schema.json` pins the shipped shape, and `validateProductSurface` catches a zero limit or missing grant if that schema is ever relaxed to a per-plan range.
- [x] 1.3 Update the platform admission policy's fixed environment set so the corrected variables are the admitted shape and the old disable-gated shape is rejected.
- [x] 1.4 Measure real embedding CPU and memory for one warm cell, and set the cell resource requests and limits from that measurement rather than the pre-embedding envelope.
  - CPU torch 2.13, `bge-base-en-v1.5`, ~280-token chunks: 531 MiB once reaped, 791 MiB warm, 918 MiB peak, 0.45 CPU-seconds per chunk at the shipped CPU batch. Envelope set to request 1Gi / limit 1536Mi, cpu 250m/2, torch threads pinned to the CPU limit. The namespace quota moved with it so the init job does not fail admission on quota.
- [x] 1.5 Build the hosted image with the embedding runtime and pre-baked weights, and prove it loads offline.
  - The `hosted` stage was built from `builder-lean`: no torch, no weights. Cells have zero egress and a read-only root, so the grant would have been inert — the cell reports ready and fails only when a tenant first queries by meaning. The build re-loads the model with the hub pinned offline after trimming duplicate serializations, and the release verifier repeats it behind `--network none`.
- [x] 1.6 Choose the encode batch size by device.
  - Batch 32 on CPU peaks at 1332 MiB for 1.8 chunks/s; batch 8 peaks at 918 MiB for 2.3 chunks/s. CPU takes 8, accelerators keep 32.
- [x] 1.7 Reject a provisioning request whose declared worker policy is not the shipped product.
  - The chart renders a fixed shape while the caller declares its own `workerPolicy`, and runtime health verifies the cell against the caller's declaration. Nothing structural kept the two in step, so a stale caller would have provisioned a working cell and then failed health with a mismatch that reads like a runtime fault.

## 2. Size the fleet to the node

- [x] 2.1 Add a failing plan test for the server type, keeping the EU location validation and destroy protection intact.
- [x] 2.2 Establish whether the node can be resized, and record the result.
  - **It cannot.** The provider lists no `cx` type as `available` or `available_for_migration` in any of its six datacenters. A reviewed saved plan (0 add, 1 change in-place, 0 destroy; policy inspector accepted, sha256 `5b738b16…54b68f7c`) failed on apply with `resource_unavailable` and left the node powered off; it was restarted and runs on `cx33`. Successors cost roughly four times as much for equivalent memory: `cpx42` (8/16 GiB) at EUR 69.49 and `ccx23` (4/16 GiB) at EUR 85.99, against `cx33` at EUR 8.49.
- [x] 2.3 Cap USER cells at four, derived from measured peak cell memory against the node in service.
- [ ] 2.4 Measure real platform overhead on the live node and revisit the cap.
  - Blocked on the platform install. The ~2.2 GiB platform figure behind the four-cell cap is the only estimate in the sizing and it is load-bearing; batch 8 alone likely supports six once it is measured.

## 3. Move vault durability to a daily cadence

- [x] 3.1 Add failing tests for the daily schedule and the 24-hour objective, including that a proposal to shorten the interval while archives remain full copies is rejected.
- [x] 3.2 Change the durability worker's schedule and every place the one-hour objective is stated, including runbooks and the observability contract's backup age thresholds.
  - Daily at 02:17 UTC; warn at 26h, block at 30h. Under a daily cadence the newest object always approaches 24h just before each run, so an alarm at the objective itself would fire every single day.
  - `capacity_rpo_met` was bounded by the interval, which under a daily cadence is 24 hours and would have passed for any sweep at all. It is now bounded by the sweep's own budget, pinned equal to the CronJob deadline by test.
- [ ] 3.3 Prove one real archive, retention, and a clean restore at the new cadence.
  - Blocked on the platform install.

## 4. Record honest economics and close the capacity gate

- [x] 4.1 Fill the capacity contract's monthly cost basis from the node in service and the daily-cadence storage figure, keeping the chart copy byte-identical.
  - Provider pricing API, fsn1: server 8.49, primary IPv4 0.50, 10 GiB volume 0.572. B2 at 5.80 is the one modelled line — worst case at full entitlement (6 cells x 30 retained daily archives x 5 GiB), with USD treated 1:1 with EUR so it over-states rather than under-states.
- [x] 4.2 Record the observed Paddle fee model and tax treatment, marking the net receipt for the friends price as derived from an observed model rather than an observed charge.
  - Observed live transaction: EUR 4.00 gross, 0.77 tax at 24%, 3.23 subtotal, 0.64 fee, 2.59 earnings. The fee solves to 5% of gross plus EUR 0.44. Net receipt at the EUR 5 friends price is 3.34; no friend has been billed.
- [x] 4.3 Flip `live_costs_verified` only once both evidence digests and every cost field are present, and confirm provisioning admission passes.

## 5. Verify

- [ ] 5.1 Run the complete pinned validation gate and strict OpenSpec validation.
- [ ] 5.2 Independent review of the rendered cell environment, the economics evidence, and the sizing argument.
