# Tasks

## 1. Preview output

- [x] 1.1 Prove that an eligible image and video produce bounded JPEGs with the bound input digest.
- [x] 1.2 Reuse the image renderer and video sampler to publish one atomic preview through the existing media worker.
- [x] 1.3 Wire preview retirement into existing media cleanup and prove that an interrupted cleanup exposes no stale output.

## 2. Preview read

- [x] 2.1 Add a `read_media` preview selection using the original proof and the live release boundaries.
- [x] 2.2 Verify that an authorized read returns the recorded output without a decoder call.
- [x] 2.3 Verify refusal after an access change and report unavailable when no current preview exists.

## 3. Delivery proof

- [x] 3.1 Measure the added lookup on populated derived state and verify its latency budget.
- [x] 3.2 Run the media and access tests, strict OpenSpec validation and public-artifact privacy checks.
