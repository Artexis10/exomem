## 1. Red-First Contract Coverage

- [x] 1.1 Add the five pure descriptor-gate regression tests and capture an expected failing run before implementation
- [x] 1.2 Add benchmark case R with a descriptor-less graph distractor and capture its expected failure before implementation
- [x] 1.3 Extend the find-envelope fixture to prove non-descriptor anchor knowledge is threaded and capture its expected failure before implementation

## 2. Descriptor-Gated Resolution

- [x] 2.1 Add optional categorical anchor descriptor knowledge to hit facts and verify pure resolver tests pass without floats or descriptorless behavior changes
- [x] 2.2 Parse at most ten anchor title/body pages through the existing corpus cache in `resolve_for_find` and verify the envelope regression passes
- [x] 2.3 Update the synthetic fixture and benchmark expectations while preserving cases A-Q, case J graph-only behavior, and all metric floors

## 3. Verification

- [ ] 3.1 Run the scoped referent, entity registry, and governance test gate from the worker brief
- [ ] 3.2 Run the benchmark, latency, Ruff, mypy, OpenSpec strict validation, and lean-shard gates from the worker brief
- [ ] 3.3 Inspect the final diff and task allowlist, record evidence in `.task/RESULT.md`, and commit only the intended scope
