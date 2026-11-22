## Context

The media worker already owns a read pin while it processes a governed original. The image viewer uses Pillow and the installed image codecs. The video reader uses PyAV to sample and encode frames. Scene frames serve a separate purpose. They illustrate changes within a video and can enter OCR and `find`.

## Goals

Produce one small visual output per eligible filing through normal media processing. Let a caller read that output with the existing proof and access rules. Keep the processing and read paths bounded.

## Decisions

### Generate through the existing media queue

Admit a private preview item in the media ledger for each new eligible original version. Use the normal capture and processing paths and prefer existing work when both are due. Preview admission depends on its own engine toggle. The worker can process a preview when the install has disabled text extraction. The companion records the eligible digest in the same write that files or rebinds the original. The existing reconciliation sweep recovers only marked current versions from its bounded census. It checks for missing ledger rows in one batched lookup per pass. A queue failure leaves the filing and its demand intact. The ledger keeps one terminal receipt for the current marked digest, including exhausted attempts. A successful receipt costs one file existence check during reconciliation. If its record is missing, reconciliation can queue that digest again.

Only the durable process-mode worker consumes preview jobs. The global media worker switch, inline debug mode and unavailable child limits can disable generation. These states admit no new preview work. Stored previews still use the same read boundary.

Verify a digest of the held stream used by the decoder. Reuse a recorded digest only when it proves that held version. Give that stream to a one-shot codec child with an enforced deadline and memory limits. Apply the preview engine budget to Cloud admission. Limit codec dimensions and secondary inputs before full decode. Reuse the image renderer and frame JPEG encoder. The child returns one bounded payload to the existing worker.

Before opening the original, check its live filing and current claim under the cross-process consistency guard. Run the codec outside the guard. Yield to foreground writers, then recheck the filing, held version and claim under the same guard before installing the record. The process worker flushes the file, shard, preview root and external state root before acknowledging publication. A refresh for the same digest and format version reuses the file. A busy publication spends one of three attempts and releases its pin before the delay. A failed preview leaves extraction results and status intact.

### Keep the preview in derived state

Write one bounded record per filing in the vault's private state. The record holds the input digest, preview format version and enough metadata to read the JPEG. Publish the record and JPEG together so a replay can identify a complete output. Replace the record on a new digest. `drop_media_residue` retires the preview for a removed filing in both storage modes. Reconciliation checks a bounded shard of preview state per pass. Recheck the filing and record under the publication guard before removal so delayed cleanup preserves a newer preview. Eviction keeps the preview.

One file per filing keeps large binary values out of the media job ledger. One JPEG per filing serves this purpose without creating a second scene-frame collection or an index entry. Keep each record at or below 64 KiB, including the JPEG. At 500,000 originals, the envelope used by replica verification, this is at most 31 GiB of payload and record data before filesystem metadata.

### Reuse the original's read boundary

`read_media` takes the canonical original path. Within the recovery slot, check the access tier, cold-blocked paths, the RAW derived-pixel floor, and path-only disclosure and connector rules. The full held proof still checks current lifecycle state, byte-bound grants and release before returning pixels. Check derived-pixel release for images and posters. Video posters also require the video frame boundary. Recheck the current binding and release immediately before returning the matching JPEG.

The action is available wherever `read_media` is offered. On Cloud that currently requires the frame engine. Preview reads use the stored output and do their decoding only in the media worker.

This applies deterministic egress containment to the new output. [Anthropic, How we contain Claude across products](https://www.anthropic.com/research/how-we-contain-claude)

Make a single keyed preview read per request, with a 25 ms p95 budget for the added lookup and bounded payload read on the local filesystem. Original verification and hydration keep their existing request budgets. The preview avoids repeated image or video decoding. It can still require the original to hydrate so the existing custody check can prove it.

## Risks and trade-offs

- Decoder failure can leave a preview unavailable. The read reports that outcome after authorization. Processing can still preserve the original and produce its other outputs.
- A preview retains sensitive pixels. It lives in the same private state scope as the media worker and follows the original's access and removal.
- A stored output can go stale after a rebind. The read checks the exact original digest, and a later authorized processing attempt replaces obsolete output.

## Migration

Enable generation through the media engine setting. The existing worker produces new previews on eligible work. After a restore omits preview records, the worker can rebuild successful previews for current marked versions.
