# Proposal

This change specifies indexed capture metadata for photos and videos. Implementation is not authorized yet; it needs its own approved plan.

## Why

A person often describes a photo by when and where it was taken: "the photos from the summer at the lake house in 2012". Exomem can already search a photo's OCR text, its image tags, its CLIP vector and the text an agent wrote on its page. It cannot use when or where the photo was taken. It reads the EXIF capture time only when it renders an image for viewing, and it never reads the GPS position or the camera. A photo with no text in it therefore has only CLIP to match it. On the live image-search set, 2 of 10 textless photos reach the first eight results.

Capture time and place are also the join that contextual retrieval needs. An event page with a date can narrow a search to the photos taken in that window.

## What Changes

- Image and video extraction records capture metadata in the sidecar frontmatter: capture time with its UTC offset when the file records one, GPS position, camera make and model, and pixel dimensions.
  - The metadata records what the file says, and leaves out what the file does not say.
  - A capture time without an offset is marked as local time with an unknown offset.
- Search filters by capture time window and by a geographic bounding box. The filters reuse the existing structured filter grammar (`$between`, `$gte`, `$lte`) and an index, so a filtered search stays bounded.
- A result shows a GPS position only to a caller who may read the original under the existing release rules.
- An owner operation adds capture metadata to existing sidecars. It rewrites only the capture block, never the extracted text. It runs only when the owner starts it, because it is a stored-data write.

This change adds no place names, no geocoding, no face detection and no change to ranking.

## Capabilities

### New Capabilities

- `media-capture-metadata`: Capture metadata extraction for images and videos, the indexed capture time and place filters, location disclosure, and the owner backfill.

## Impact

The implementation extends image and video extraction, the sidecar writer, the structured filter fields and the metadata index. It reuses the existing EXIF reader and the media job ledger. It adds no dependency beyond the image library and the video probe the media worker already uses.
