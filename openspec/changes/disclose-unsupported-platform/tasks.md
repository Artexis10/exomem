## 1. Separate the two questions

- [x] 1.1 Add `platform_supported()` to `_held_fs_posix` beside the Linux rule, with the reason darwin needs a second implementation rather than a relaxed predicate.
- [x] 1.2 Add the symmetric `platform_supported()` to `_held_fs_windows`.
- [x] 1.3 Add `held_fs.PlatformSupport` and `held_fs.platform_support()`, naming the platform and the served platforms in the reason.
- [x] 1.4 Make `_probe` call the new predicate so one platform rule exists, and pin that with a test that patches the predicate and observes `_probe`.
- [x] 1.5 Assert `acquire` still refuses with `CAPABILITY_UNAVAILABLE` and substitutes no weaker route.

## 2. Refuse once, stay answerable

- [x] 2.1 Refuse a vault-touching command in `_run_cli` with one message naming the platform and pointing at the doctor.
- [x] 2.2 Keep `doctor`, `install-info`, and bare flags reachable.
- [x] 2.3 Assert a supported host is never refused.

## 3. Explain it

- [x] 3.1 Add the `platform.held_filesystem` doctor check as a failure with remediation.
- [x] 3.2 Assert it passes where a backend exists and carries no remediation there.

## 4. Stop claiming it

- [x] 4.1 Replace `Operating System :: OS Independent` with Linux and Windows, with the reason in a comment.
- [x] 4.2 Assert the classifiers in a test, so the claim cannot quietly return.
- [x] 4.3 Correct README and `docs/deployment.md`; label the launchd recipe rather than deleting it.

## 5. Report the refusal as a skip, narrowly

- [x] 5.1 Add `has_held_filesystem_backend()` and `declares_absent_held_filesystem()` to `tests/benchmark_capabilities.py`.
- [x] 5.2 Add the gated branch to `pytest_runtest_makereport`.
- [x] 5.3 Assert every substrate refusal string taken verbatim from a red macOS shard is recognised, and that the graph-epoch cascade and a bare failure are not.
- [x] 5.4 Assert the matcher walks an exception chain, since `pytest.raises(match=...)` re-raises as `AssertionError`.

## 6. Verify

- [x] 6.1 Run the new suite plus `tests/test_doctor.py`, `tests/test_held_fs_contract.py`, `tests/test_reserved_admin_paths.py`, `tests/test_cli_core_ops.py`, `tests/test_cli_lazy_imports.py`.
- [x] 6.2 Run `openspec validate disclose-unsupported-platform --strict` and `openspec validate --specs --strict`.
- [x] 6.3 Confirm no tool-surface artifact moved.
- [x] 6.4 Run lint.

## 7. Closure

- [ ] 7.1 Once merged and therefore demonstrably shipped, sync the delta into `openspec/specs/` and archive with `openspec archive`, re-running `openspec validate --all --strict` before and after.
- [ ] 7.2 Follow-up, not filed here because it needs a macOS host to characterise: measure what remains red on a macOS shard after the skip branch, and decide whether the graph-epoch cascade warrants its own treatment or whether the matrix entry should say "unserved" instead of running the suite.
