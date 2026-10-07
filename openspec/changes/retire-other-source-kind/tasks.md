## 1. Capture contract

- [x] 1.1 Refuse a missing kind, `other` and `unclassified` on agent-facing capture with `SOURCE_KIND_REQUIRED`, the known kinds with counts, and the rule line, before any fetch or write
- [x] 1.2 Record a missing kind as `unclassified` on the terminal UI, the hosted private command router, the upload form and legacy-vault import
- [x] 1.3 Deprecate `other`, add the `unclassified` built-in, and refuse both as a reclassification target
- [x] 1.4 Report classification debt through `structure_suggestion: source_classification_debt` and bound its new payload in the committed terminal
- [x] 1.5 Name the alternative in the URL refusal
- [x] 1.6 Carry the refusal guidance and the known kinds across the hosted boundary

## 2. Agent-facing text

- [x] 2.1 Replace bootstrap's fallback rule with the kind rule
- [x] 2.2 Remove `Other` as a destination from the scaffold references, the capture and ingest skills, `source-taxonomy.yaml`, the knowledge packs and the docs
- [x] 2.3 Regenerate the skill contract, packaged skills, tool schemas, hosted plugins, cloud plugin and capabilities document

## 3. Proof

- [x] 3.1 Red-then-green tests for each behaviour in `tests/test_source_kind_required.py` and the two hosted route tests
- [x] 3.2 Update tests that encoded the fallback contract
- [ ] 3.3 Full pytest corpus green in pull-request CI

## 4. Delivery

- [ ] 4.1 Independent review, merge, then sync this delta and run `openspec archive`
