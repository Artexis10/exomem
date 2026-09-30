## 1. The band yields to the retrieval carry

- [x] 1.1 Red-first: the T6 known-limit test becomes a test of the rule (on arm: the grill page is not resolved and the packet equals the off arm's carried packet), and the English-set invariant no longer skips T6. Both fail on the prior code because the grill page resolves on `rare_term` + `vector_band`.
- [x] 1.2 `working_set_resolve.band_yieldable_paths`, with unit tests: a band-only resolution on an ordinary word is yieldable, one on a name is not, a page resolved on more than the band is not, and one anchor resolved on its own words means the band decided nothing.
- [x] 1.3 `compile_packet` asks the retrieval carry on a yieldable turn and serves the carried packet when the carry names a different dominant page and the packet builds; otherwise the band's resolution stands.
- [x] 1.4 Review round: red-first, an incidental note the carry names beside M1-de and M1-ru cost the gold and served nothing. Core-tier tests pin the name gate (a capitalised name is not asked about) and the build check (an unbuildable carried page keeps the band's page); the embeddings file adds the incidental notes and M1-de, M1-ru and M5-ja keep resolving.
- [x] 1.5 Verified on bge-m3: the multilingual embeddings file passes 12/12 (T6 on equals off, M1-de, M1-ru and M5-ja keep resolving their gold, English set 18/18 with no exclusion); the real-compiler corpus stays 9/18 and continuity 4/4.
- [ ] 1.6 After merge, sync the delta into `openspec/specs/context-activation/spec.md` and archive this change.
