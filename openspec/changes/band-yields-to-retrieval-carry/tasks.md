## 1. The band yields to the retrieval carry

- [x] 1.1 Red-first: the T6 known-limit test becomes a test of the rule (on arm: the grill page is not resolved and the packet equals the off arm's carried packet), and the English-set invariant no longer skips T6. Both fail on the prior code because the grill page resolves on `rare_term` + `vector_band`.
- [x] 1.2 `working_set_resolve.band_decided_paths` and `band_yielded`, with unit tests: band-only resolution is detected, a page resolved on more than the band is not, one anchor resolved on its own words means the band decided nothing, and a yielded resolution holds its pages at `partial`.
- [x] 1.3 `compile_packet` asks the retrieval carry on a band-decided turn and yields when it names a different dominant page, reusing the carry's answer for the ordinary carry.
- [x] 1.4 Verified on bge-m3: the multilingual embeddings file passes 11/11 (T6 on equals off, M1-de, M1-ru and M5-ja keep resolving their gold, English set 18/18 with no exclusion); the real-compiler corpus stays 9/18 and continuity 4/4.
- [ ] 1.5 After merge, sync the delta into `openspec/specs/context-activation/spec.md` and archive this change.
