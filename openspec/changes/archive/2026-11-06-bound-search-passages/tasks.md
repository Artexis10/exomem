## 1. Bound the passage

- [x] 1.1 Cut a passage longer than `MAX_PASSAGE_CHARS` around the start of the match, and keep `offset` and `length` true to the shown text.
- [x] 1.2 Verify that a log without word breaks returns a bounded passage that contains the match and that `read_memory` at its offset opens the match.
