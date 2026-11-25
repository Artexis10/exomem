## Why

When a conversation is about a photo, Exomem often fails to find it. On a blind set of 21 photos with no readable text, the right photo was in the top 8 for 11 of them. A ranking change that helps photo questions in an ordinary search has always cost text questions, and scores alone cannot show that a question wants a picture. The calling agent can tell from the conversation, but `search_memory` offers it no plain way to say "pictures only".

The index can already restrict a search to images: `page.file_type` is an indexed filter axis. Inside that restriction, though, the CLIP lane still applies its chance band. The band is measured over the whole index and withholds almost every image for a weakly matching query, so a restricted search ranks photos by their OCR text and tags alone. A live read-only trace on 2026-11-24 showed the band withholding 1,016 pages inside an image-only filter.

## What Changes

- `search_memory` takes `file_types`, a list of file kinds such as `["image", "video"]`. It reaches the existing `file_types` shortcut, which compiles to `page.file_type`. Its description tells the agent to set it when the conversation is about a photo, screenshot or video, and to search both with and without it when unsure. The calling model's judgement is the only intent signal; no word list decides it.
- When the caller's filters admit only the sidecars of images and videos, the CLIP lane runs without the chance band. In fusion, the CLIP lane then takes a ranking setting, `clip_media_restricted_weight`, in place of its intent weight. The text lanes still vote among the pictures.
- Every search whose filters admit anything other than images and videos keeps today's band and weights unchanged.
- The CLIP lane trace reports `media_restricted`.

Out of scope: inferring visual intent inside Exomem, indexing the text of notes that link a photo into that photo's search text, capture-date backfill, and the reranker's treatment of textless images.

## Capabilities

### Modified Capabilities

- `search-lane-relevance`: the CLIP band applies to every search except one restricted to pictures, and `search_memory` can restrict a search to file kinds.

## Impact

- Code: `src/exomem/public_tools.py`, `src/exomem/find_candidates.py`, `src/exomem/ranking_config.py`, `src/exomem/media_types.py`, `src/exomem/media_processing.py`.
- Public surface: one new optional `search_memory` parameter. The MCP discovery fingerprint changes, so hosted connectors need a refresh after release.
- Behaviour: only searches restricted to pictures rank differently.
