## 1. Regression coverage

- [x] 1.1 Prove a version bump alone leaves the descriptor and `compatibility_sha256` unchanged.
- [x] 1.2 Prove a contract-surface change still moves `compatibility_sha256`.
- [x] 1.3 Prove the descriptor contains the Exomem release under no key.

## 2. Decouple the identity

- [x] 2.1 Remove `source_release` from the definition, its schema, and its validation.
- [x] 2.2 Exclude the Exomem release from the descriptor and its hashed base.
- [x] 2.3 Drop the definition/contract release equality guard.
- [x] 2.4 Regenerate the committed hosted artifacts.

## 3. Retire the release-branch resync

- [x] 3.1 Remove `scripts/sync_hosted_release.py` and its tests.
- [x] 3.2 Remove the `sync-hosted-artifacts` resync step from the release workflow.

## 4. Verification

- [x] 4.1 Run focused hosted plugin tests, Ruff, and strict OpenSpec validation.
- [x] 4.2 Run the lean suite.
- [x] 4.3 Confirm a simulated version bump produces no artifact diff.

### Verification evidence

- `pytest tests/test_hosted_plugin_definition.py tests/test_hosted_plugin_rendering.py
  tests/test_hosted_plugin_promotion.py tests/test_hosted_plugin_release_identity.py`
  — 44 passed.
- `uvx ruff check src/exomem/hosted_plugins.py tests/test_hosted_plugin_release_identity.py`
  — clean.
- Simulated release: with `__version__` and `pyproject.toml` moved 0.34.0 -> 0.99.0,
  `hosted-plugin.py regenerate --platform claude` produced byte-identical
  `compatibility.json` (`5368d48b…`) and `claude.lock.json` (`4ce1c1e1…`), and
  `check --platform claude` reported "Hosted generated artifacts are current"
  while the bumped version was installed.
- A fourth coupling surfaced during implementation and is covered: the contract's
  own digest hashes a base containing `exomem_release`, so `schema_contract_sha256`
  had to be recomputed over the published contract. Caught by 1.1, not by review.

### Deliberately out of scope

The **runtime** agent gateway contract still folds `exomem_release` into
`contract["digest"]`, so a client caching that contract by digest is still
invalidated by an unrelated patch release. Same defect class, different surface.
`schema_contract_sha256` never leaves `hosted_plugins`, so fixing it here needed
no runtime change; fixing the runtime digest is a protocol-visible change and
belongs to its own proposal.
