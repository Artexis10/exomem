# Tasks

## 1. Preview output

- [x] 1.1 Prove that an eligible image and video produce bounded JPEGs with the bound input digest.
- [x] 1.2 Run preview codec work under separate limits in the existing queue and retry deferred publication within a bounded budget.
- [x] 1.3 Wire preview retirement into existing media cleanup and prove that an interrupted cleanup exposes no stale output.
- [x] 1.4 Prove cross-process publication and reconstruction of a missing successful preview in a store-mode vault.
- [x] 1.5 Prove recovery after queue admission fails, terminal retry limits, the global disable and the absence of backfill.
- [x] 1.6 Prove Source capture and first-time filing through startup and periodic discovery.
- [x] 1.7 Prove bounded cleanup reads, confirmed directory replacement and revalidation before deletion.
- [x] 1.8 Prove interrupted final-attempt acknowledgement and delayed return after hydration contention.

## 2. Preview read

- [x] 2.1 Check the canonical original's byte-free denial rules before hydration and recheck the held proof before release.
- [x] 2.2 Verify that an authorized read returns the recorded output without a decoder call.
- [x] 2.3 Verify refusal after an access change and report unavailable when no current preview exists.

## 3. Delivery proof

- [x] 3.1 Measure the added lookup on populated derived state and verify its latency budget.
- [x] 3.2 Run the media, access and discovery tests, strict OpenSpec validation and public-artifact privacy checks.
