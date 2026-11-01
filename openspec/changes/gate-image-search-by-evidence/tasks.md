## 1. OCR keeps confident words searchable

- [ ] 1.1 `ocr_models`: return the chosen pass's text, per-word confidences and mean confidence; split kept and dropped words by the floor, removing dropped words from Tesseract's own text lines so kept words keep their spacing.
- [ ] 1.2 Floor setting (default 70, Tesseract 0-100 scale) in one place with a one-line reason; 0 keeps every word.
- [ ] 1.3 `extract._ocr_image` and the frame path use the split; PDF page OCR keeps whole text.
- [ ] 1.4 `preserve`: render `ocr_confidence` and the `## Low-confidence OCR` section; the heading lives in one constant.
- [ ] 1.5 `find_types.RankingPage.search_body`: omit the section for media sidecars.
- [ ] 1.6 Tests: a word only in the low-confidence section does not match a search; the sidecar keeps the dropped words and records `ocr_confidence`; CJK spacing survives a partial drop.

## 2. CLIP lane admits only matches above chance

- [ ] 2.1 Apply `semantic_band` to the full brute-force CLIP population before top-k; band alpha and minimum population are `RankingConfig` settings; uncalibrated keeps the old lane.
- [ ] 2.2 Trace reports withheld counts; cache keys include the new settings.
- [ ] 2.3 Test: a noise-level CLIP match does not outrank a matching note; a clear outlier still ranks.

## 3. Intent routing without leading words

- [ ] 3.1 Remove `EXACT_LEADING` routing from `find_policy.classify_intent`; keep quotes and wikilinks.
- [ ] 3.2 Update intent tests to the new contract.

## 4. Delivery

- [ ] 4.1 Golden retrieval floors stay green; scoped suites and full suite pass.
- [ ] 4.2 Post-release: rerun the 24-query set and textless-photo queries live; per-case before/after on the same keys.
