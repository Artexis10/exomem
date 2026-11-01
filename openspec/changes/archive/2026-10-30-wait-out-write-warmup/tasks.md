## 1. Implementation

- [x] 1.1 Wait out a warming write-gate component for keyed writes, bounded like a busy boundary
- [x] 1.2 Test that a keyed write during warm-up commits once, and that a warm-up longer than the bound gives the retryable refusal
