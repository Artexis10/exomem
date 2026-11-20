## Context

Measured on 2026-11-01 against the live personal vault, read-only. Answer keys came from vault pages read directly, never from search output. Ranking levers were replayed offline from the live per-lane ranks, and the replay reproduced the live order exactly for all 24 queries.

## Decisions

### D1. Filter OCR by word, not by image

Whole-image mean confidence separates photos (33-44) from screenshots (67-93), but it does not separate clean from garbled screenshots. On 16 sampled images with reference transcriptions, a per-word floor of 70 raised token precision by 0.25 (95% CI 0.11 to 0.42) and lowered recall by 0.03 (CI 0.01 to 0.05). A line-level floor did worse on both. A 2x upscale and PSM 6 did not pay.

### D2. Keep low-confidence words in the sidecar

The operator chose to keep low-confidence text in the file and out of the index. The words move to a named sidecar section. `find_types.RankingPage.search_body` is the one place every search field reads, so it omits the section for media sidecars. The section heading and the `exomem:sidecar-low-confidence-ocr` marker that precedes it are defined once and imported where they are read. A heading proves no ownership, since OCR text can hold any line, so the reader cuts on the marker. A re-rendered sidecar drops the section instead of moving it into the preserved notes, so the withheld words do not become searchable there; a reprocess reads them again.

### D3. Keep Tesseract's own spacing

Rebuilding lines from TSV words would lose the CJK spacing that `preserve_interword_spaces` protects. Each dropped word is removed from Tesseract's own text line in reading order, so kept words keep their original spacing.

### D4. Gate CLIP by a corpus-relative band

On text questions the CLIP lane added 0.31 unrelated media per top 8 (CI 0.12 to 0.56) and changed no visual hit. CLIP cosines for text and visual queries overlap (medians 0.254 and 0.280), so a fixed cosine separates them poorly and would belong to one model. `chance_band.semantic_band` (moved out of the working set, same code) already finds the chance maximum from the population's median and MAD. The brute-force CLIP path scores every row of the index, video keyframes included, so it applies the band to that population before the request's filters and before it takes the top k. The KNN backend returns only k rows, so the band never uses it. An alpha outside (0, 1) has no chance level, so the lane runs without the band and traces `off`. The lane fetches k rows, the k highest-scoring index rows among the request's allowed images, where k is 8 times the candidate count. The trace counts the distinct pages in that fetch that the band removed.

Offline replay on the per-question chance levels of a read-only scoring run over all 1048 CLIP index rows, with bootstrap 95% intervals on per-question differences. The set held 16 text questions, 8 screenshot questions and 10 questions for photos with no readable text:

- Text questions: the highest CLIP score of every question is below its chance level, so the band withholds the lane. Irrelevant media in the top 8 fell by 0.375 (CI -0.625 to -0.125), and nDCG@8 did not change.
- With the D5 routing change as well: nDCG@8 rose by 0.035 (CI 0.009 to 0.066), and irrelevant media in the top 8 fell by 0.688 (CI -1.062 to -0.312).
- Screenshot questions: the target stays in the top 8 for 6 of 8, as before.
- Photos with no readable text: the target stays in the top 8 for 2 of 10, as before. With the CLIP lane off it falls to 0 of 10, so the band keeps the lane where the picture is the answer.

### D5. Drop leading-word intent

A word regex that decides what a person meant is an unsound pattern. On top of D4, routing those questions as conceptual raised text nDCG@8 by 0.038 (CI 0.004 to 0.087) and turned one visual miss into a hit.

## Risks

- CJK and vertical text may score lower confidence. The sample held no Japanese screenshot. The floor is a setting, and the post-release check includes the operator's own queries.
- The CLIP population is every index row, so a vault with many video keyframes calibrates on a mostly-keyframe distribution. The post-release check reads the `withheld_pages` count on both kinds of query.

## Follow-ups

- The governed projected CLIP lane (`governance/projected_retrieval.py`, reached through `projection_runtime.py`) ranks its projected image vectors without the band. It needs its own decision, since its population is the projected set that a reader may see.
- Display readers (`memory_context.py`, `context_pack.py`) still show a sidecar's `## Low-confidence OCR` section as page text. They are not search fields.
- One non-finite CLIP row makes the band report `uncalibrated` for the whole lane.
