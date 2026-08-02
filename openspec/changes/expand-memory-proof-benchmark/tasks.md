# Tasks

## 1. Registry and contracts
- [x] 1.1 Family registry module (benchmarks/membench/families.py: 7 active v0.1 + 8 planned v0.2 + tacit_polanyi out-of-scope; generation-time refusal at the single choke point; taxonomy-membership + integrity guards) — reviewed APPROVE, 16 tests, release-manifest byte-identity unchanged
- [x] 1.2 Coverage table published in docs/memory-proof-benchmark.md with verbatim no-drift gate
- [ ] 1.3 Provider onboarding doc + third-party adapter conformance suite (faked runtime seam)

## 2. Governance wiring (closes the v0.1 gap)
- [ ] 2.1 PolicySet → `_Governance/` translation in the exomem adapter via public surfaces; persona threading in the runner; GOVERNED_VIEWS declared when wired
- [ ] 2.2 Three-state governance reporting (wired / default-open-labelled / unsupported) + tests incl. restricted-vs-owner persona round-trip
- [ ] 2.3 Re-run the governance family wired; record wired-vs-default-open delta in the findings doc

## 3. New families (fable lanes after 1.1; red-first per family)
- [x] 3.1 Procedural/how-to chains — t17 + membench/procedural.py; delegated lane, independent review APPROVE (no HIGH/MEDIUM); integrated 0f1e4d3
- [x] 3.2 Quantitative reasoning — t18 + membench/quant.py (Decimal-only, unit table, tolerance); delegated lane, review APPROVE (no blocking); integrated with this commit
- [x] 3.3 Negation & counterfactuals — t19 over existing DISPROVED/REVOKED; delegated lane, review APPROVE (no HIGH/MEDIUM); integrated ff2d324
- [ ] 3.4 Cross-lingual facts (non-Latin syllabary wordbank extension; unsupported-not-zero profile handling)
- [ ] 3.5 Preference attribution + source-reliability (behavioural hedging, correction-history citations)
- [ ] 3.6 Long-horizon 52-week entropy release + quarterly health snapshots
- [ ] 3.7 Multimodal depth under the media profile (real PDF, OCR image, audio transcript; degradation without extras)

## 4. Industry-standard packaging
- [ ] 4.1 Replication kit (pinned seeds, one-command regeneration, hash verification) proven on a clean checkout
- [ ] 4.2 Versioned corpus releases + changelog; held-out seed documented
- [ ] 4.3 Publication gate implementation in reporting (judged-dimension block without agreement data; profile/version/replication labels on every figure)
- [ ] 4.4 Judge–human agreement measurement protocol + first measurement on a balanced sample

## 5. Validation
- [ ] 5.1 `openspec validate expand-memory-proof-benchmark --strict`
- [ ] 5.2 Lean membench suite green incl. new family tests (<60s each, offline)
- [ ] 5.3 Determinism: double-generation equality incl. new families; template isolation unchanged for v0.1 templates
- [ ] 5.4 Full-suite corpus regeneration matches an updated committed release manifest
