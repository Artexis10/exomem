## 1. Pack tension on a cold matrix

- [x] 1.1 Read proximity pairs from `EmbeddingIndex.require_resident_matrix()` in a managed runtime, and let `CorpusMatrixCold` reach the pack.
- [x] 1.2 Report a still-loading matrix in the pack's `truncation`, with `embeddings_available: false`.
- [x] 1.3 Test that a cold matrix starts one background load without an inline load, and that the pack names the loading matrix.
