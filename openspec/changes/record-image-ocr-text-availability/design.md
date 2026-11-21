## Decisions

Measure text availability at the output of the existing confidence filter. The field describes what OCR retained before the extractor adds enrichment.

Pass the optional boolean through the existing durable result and write it with the OCR facts. The public page reader already returns this frontmatter.

## Compatibility

Omit the field when no measurement exists. The job store checks the hash of the canonical payload, so pending results must keep their original payload shape.

Reprocessing replaces the current OCR fields and moves any old measurement to the prior extraction page. Existing sidecars retain their stored content.

## Rollback

For a planned rollback, the release owner uses the existing quiesce-and-drain procedure. The upgraded runtime publishes queued media results while their producers are quiesced. The release owner verifies `result_publication.pending_count` is zero before starting the previous runtime.

An emergency rollback can leave a queued result with `ocr.text_available`. The previous runtime discards that result and marks the matching running job failed with `media result payload rejected`. The original and any committed sidecar bytes survive. Retry an unfinished first extraction explicitly. For an unfinished reprocess, submit a new reprocess request. Both operations read the preserved original.
