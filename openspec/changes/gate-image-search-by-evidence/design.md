## Context

Measured on 2026-11-01 against the live personal vault, read-only. Answer keys came from vault pages read directly, never from search output. Ranking levers were replayed offline from the live per-lane ranks, and the replay reproduced the live order exactly for all 24 queries.

## Decisions

### D1. Filter OCR by word, not by image

Whole-image mean confidence separates photos (33-44) from screenshots (67-93), but it does not separate clean from garbled screenshots. On 16 sampled images with reference transcriptions, a per-word floor of 70 raised token precision by 0.25 (95% CI 0.11 to 0.42) and lowered recall by 0.03 (CI 0.01 to 0.05). A line-level floor did worse on both. A 2x upscale and PSM 6 did not pay.

### D2. Keep low-confidence words in the sidecar

The operator chose to keep low-confidence text in the file and out of the index. The words move to a named sidecar section. `find_types.RankingPage.search_body` is the one place every search field reads, so it omits the section for media sidecars. The section heading is defined once and imported where it is read.

### D3. Keep Tesseract's own spacing

Rebuilding lines from TSV words would lose the CJK spacing that `preserve_interword_spaces` protects. Each dropped word is removed from Tesseract's own text line in reading order, so kept words keep their original spacing.

### D4. Gate CLIP by a corpus-relative band

On text questions the CLIP lane added 0.31 unrelated media per top 8 (CI 0.12 to 0.56) and changed no visual hit. CLIP cosines for text and visual queries overlap (medians 0.254 and 0.280), so a fixed cosine separates them poorly and would belong to one model. `working_set_resolve.semantic_band` already finds the chance maximum from the population's median and MAD. The brute-force CLIP path scores every image, so it applies the band to that population before taking the top k.

### D5. Drop leading-word intent

A word regex that decides what a person meant is an unsound pattern. On top of D4, routing those questions as conceptual raised text nDCG@8 by 0.038 (CI 0.004 to 0.087) and turned one visual miss into a hit.

## Risks

- CJK and vertical text may score lower confidence. The sample held no Japanese screenshot. The floor is a setting, and the post-release check includes the operator's own queries.
- The CLIP band is not replayable offline. The post-release check reruns the visual queries plus queries for textless photos.
