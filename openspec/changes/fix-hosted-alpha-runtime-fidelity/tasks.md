## 1. Restore the complete product surface in hosted cells

- [ ] 1.1 Add a failing rendered-manifest test asserting the cell environment does not contain `EXOMEM_DISABLE_EMBEDDINGS` or `EXOMEM_DISABLE_MEDIA_EXTRACTION`, and that the worker limit is greater than zero with the `embeddings` grant present.
- [ ] 1.2 Set the cell chart's worker limit and feature grants accordingly, and make rendering fail closed when a zero worker limit would silently produce keyword-only recall.
- [ ] 1.3 Update the platform admission policy's fixed environment set so the corrected variables are the admitted shape and the old disable-gated shape is rejected.
- [ ] 1.4 Measure real embedding CPU and memory for one warm cell, and set the cell resource requests and limits from that measurement rather than the pre-embedding envelope.

## 2. Resize the node to hold six embedding-capable cells

- [ ] 2.1 Add a failing plan test for the resized server type, keeping the EU location validation and destroy protection intact.
- [ ] 2.2 Change the foundation server type and its validation, then produce a reviewed saved plan and confirm it contains no destroy or replacement of the network, primary IP, firewall, SSH key, or tunnel.
- [ ] 2.3 Apply the reviewed plan and retain redacted plan, locking, and cost evidence.

## 3. Move vault durability to a daily cadence

- [ ] 3.1 Add failing tests for the daily schedule and the 24-hour objective, including that a proposal to shorten the interval while archives remain full copies is rejected.
- [ ] 3.2 Change the durability worker's schedule and every place the one-hour objective is stated, including runbooks and the observability contract's backup age thresholds.
- [ ] 3.3 Prove one real archive, retention, and a clean restore at the new cadence.

## 4. Record honest economics and close the capacity gate

- [ ] 4.1 Fill the capacity contract's monthly cost basis from the resized node and the daily-cadence storage figure, keeping the chart copy byte-identical.
- [ ] 4.2 Record the observed Paddle fee model and tax treatment, marking the net receipt for the friends price as derived from an observed model rather than an observed charge.
- [ ] 4.3 Flip `live_costs_verified` only once both evidence digests and every cost field are present, and confirm provisioning admission passes.

## 5. Verify

- [ ] 5.1 Run the complete pinned validation gate and strict OpenSpec validation.
- [ ] 5.2 Independent review of the rendered cell environment, the saved plan, and the economics evidence.
