## Context

The media worker already owns a read pin while it processes a governed original. The image viewer uses Pillow and the installed image codecs. The video reader uses PyAV to sample and encode frames. Scene frames serve a separate purpose. They illustrate changes within a video and can enter OCR and `find`.

## Goals

Produce one small visual output per eligible filing through normal media processing. Let a caller read that output with the existing proof and access rules. Keep the processing and read paths bounded.

## Decisions

### Generate inside the media worker

Use the existing admission, original pin and host resource limits before extraction can hand off the job. Apply this to original image and video filings. Scene frames retain their own processing path. Reuse the image renderer for orientation and color conversion. Reuse the frame sampler and JPEG encoder for video. Exomem adds the storage and access rules that the decoders lack.

Verify a digest of the held stream used by the decoder. Reuse a recorded digest only when it proves that held version. Decode outside the mutation boundary. Under the existing publication guard, recheck the filing and held source version before installing the record. A refresh for the same digest and preview format version reuses the file. A decode failure leaves other media stages free to finish.

### Keep the preview in derived state

Write one bounded record per filing in the vault's private state. The record holds the input digest, preview format version and enough metadata to read the JPEG. Publish the record and JPEG together so a replay can identify a complete output. Replace the record on a new digest. `drop_media_residue` retires the preview for a removed filing in both storage modes. Reconciliation checks a bounded shard of preview state per pass. Recheck the filing and record under the publication guard before removal so delayed cleanup preserves a newer preview. Eviction keeps the preview.

One file per filing keeps large binary values out of the media job ledger. One JPEG per filing serves this purpose without creating a second scene-frame collection or an index entry. Keep each record at or below 64 KiB, including the JPEG. At 500,000 originals, the envelope used by replica verification, this is at most 31 GiB of payload and record data before filesystem metadata.

### Reuse the original's read boundary

`read_media` takes the original selection. Check the canonical original's access tier and use the existing recovery path to prove its input and grant full release. Apply the derived-pixel floor to images and posters. Video posters also require the video frame boundary. Recheck the current binding and release immediately before returning the matching JPEG.

The action is available wherever `read_media` is offered. On Cloud that currently requires the frame engine. Preview reads use the stored output and do their decoding only in the media worker.

This applies deterministic egress containment to the new output. [Anthropic, How we contain Claude across products](https://www.anthropic.com/research/how-we-contain-claude)

Make a single keyed preview read per request, with a 25 ms p95 budget for the added lookup and bounded payload read on the local filesystem. Original verification and hydration keep their existing request budgets. The preview avoids repeated image or video decoding. It can still require the original to hydrate so the existing custody check can prove it.

## Risks and trade-offs

- Decoder failure can leave a preview unavailable. The read reports that outcome after authorization. Processing can still preserve the original and produce its other outputs.
- A preview retains sensitive pixels. It lives in the same private state scope as the media worker and follows the original's access and removal.
- A stored output can go stale after a rebind. The read checks the exact original digest, and a later authorized processing attempt replaces obsolete output.

## Migration

Enable generation through the media engine setting. The existing worker produces new previews on eligible work. Install rollback can discard and rebuild the derived records from the bound originals.
