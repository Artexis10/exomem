# Proposal

This change specifies indexed capture metadata for photos and videos. Implementation is not authorized yet; it needs its own approved plan.

## Why

A person often describes a photo by when and where it was taken: "the photos from that summer at the lake in 2012". Exomem can already search a photo's OCR text, its image tags, its CLIP vector and the text an agent wrote on its page. It cannot use when or where the photo was taken. It reads the EXIF capture time only when it renders an image for viewing, and it never reads the GPS position or the camera. A photo with no text in it therefore has only CLIP to match it. On the maintainers' image-search evaluation set (17 Nov 2026), 2 of 10 textless photos reached the first eight results.

Capture time and place are also the join that contextual retrieval needs. An event page with a date can narrow a search to the photos taken in that window.

## What Changes

- Image and video extraction records capture metadata in the sidecar frontmatter: the recorded capture time with its UTC offset when the file records one, the local capture date, GPS position, camera make and model, and pixel dimensions.
  - The metadata records what the file says, and leaves out what the file does not say.
  - For images, only the EXIF original capture time counts, never the modification time that a scanner sets.
- Search filters by capture date and by a geographic bounding box. The filters reuse the existing ordered operators on ISO dates and numbers over an index, so a filtered search stays bounded.
- A caller who may not read the original sees no position on any surface, filters included.
- An owner operation adds capture metadata to existing sidecars. It rewrites only the capture block, never the extracted text. It runs only when the owner starts it, because it is a stored-data write.

This change adds no place names, no geocoding, no face detection and no change to ranking.

## Capabilities

### New Capabilities

- `media-capture-metadata`: Capture metadata extraction for images and videos, the indexed capture date and place filters, location withholding, and the owner backfill.

### Modified Capabilities

- `structured-retrieval-filters`: A field withheld from the caller for a page reads as missing in every filter, generic frontmatter pointers included.

## Impact

The implementation extends image and video extraction, the sidecar writer, the structured filter fields and the metadata index. It reuses the existing EXIF reader and the media job ledger. It adds no dependency beyond the image library and the video probe the media worker already uses.
