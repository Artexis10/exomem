## 1. The band yields to the retrieval carry

- [x] 1.1 Red-first: the T6 known-limit test becomes a test of the rule (on arm: the grill page is not resolved and the packet equals the off arm's carried packet), and the English-set invariant no longer skips T6. Both fail on the prior code because the grill page resolves on `rare_term` + `vector_band`.
- [x] 1.2 `working_set_resolve.band_yieldable_paths`, with unit tests: a band-only resolution on an ordinary word is yieldable, one on a name is not, a page resolved on more than the band is not, and one anchor resolved on its own words means the band decided nothing.
- [x] 1.3 `compile_packet` asks the retrieval carry on a yieldable turn and serves the carried packet when the carry names a different dominant page and the packet builds; otherwise the band's resolution stands.
- [x] 1.4 Review round: red-first, an incidental note the carry names beside M1-de and M1-ru cost the gold and served nothing. Core-tier tests pin the name gate (a capitalised name is not asked about) and the build check (an unbuildable carried page keeps the band's page); the embeddings file adds the incidental notes and M1-de, M1-ru and M5-ja keep resolving.
- [x] 1.5 Verified on bge-m3: the multilingual embeddings file passes 12/12 (T6 on equals off, M1-de, M1-ru and M5-ja keep resolving their gold, English set 18/18 with no exclusion); the real-compiler corpus stays 9/18 and continuity 4/4.
- [x] 1.6 After merge, sync the delta into `openspec/specs/context-activation/spec.md` and archive this change.

## 2. Task-relevant carry refinement

- [x] 2.1 Red-first on actual packets: reject unrelated property-body-contact units and prose pointers on both evidence arms; preserve an independently named shared-word page occurrence and possessive authored page titles.
- [x] 2.2 Demote only the affected ordinary-word band candidate before branching, without empty-carry resurrection or interference from a separately resolved anchor; preserve the existing multilingual name/casing protection.
- [x] 2.3 Verify rich subject/category context versus an unrelated sibling, existing standing/body-only routes, unchanged corpus/scorer and scoped completion tests; obtain independent review and report remaining grammar/quality limits before ordinary delivery.

Source delivery: PR #1553 merged as `315dca1daf8ca8aec920c6c556952acc23b61358` after required CI passed in run `37194381250`. Author-independent review approved the occurrence-local correction; 2,617 compiler/context tests and 12 offline BGE-M3 cases passed. A freshly built and installed 0.104 package preserved the scoped negative, independent context and genuine ambiguity through the public activation operation. The unchanged corpus remains raw 9/18 and A8/A9 10/18; this does not establish overall quality uplift, deployed-release acceptance or original-client incident closure.
