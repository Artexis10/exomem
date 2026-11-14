# Tasks

## 1. Cut identity and evidence

- [x] 1.1 Add source, predecessor, required-check and retry cases to `tests/test_release_evidence.py`; expect failures before wiring.
- [x] 1.2 Extend `scripts/release_evidence.py` for stable cut identities and inherited check policy; expect scoped identity/evidence tests to pass.
- [x] 1.3 Narrow JSON validation and route Cloud/plugin-sync checks; expect tampering and wrong release versions to fail before tagging.
- [x] 1.4 Document source selection and per-cut failures in `docs/release.md`; expect commands to select immutable identities without global holds.

## 2. Branch release and main reconciliation

- [x] 2.1 Route Release Please through main controllers targeting isolated branches; expect main pushes to create neither proposals nor releases.
- [x] 2.2 Extend release scope and evidence automation for cut PRs; expect configured checks and one matching Full producer.
- [x] 2.3 Verify squash parents/trees before native tag creation; expect changed cut content to stop without retagging.
- [x] 2.4 Reconcile current metadata, dependency selections, generated packages and the boundary together; expect intervening main changes to survive.
- [x] 2.5 Persist checked origins and extend publisher recovery; expect missing transient state to remain pending and conflicting immutable identities to fail.
- [x] 2.6 Document migration, recovery and rollback in `docs/release.md`; expect one resumable cut operation and no manual phase sequence.

## 3. Integration and delivery

- [x] 3.1 Run the disposable two-cut proof with pinned Release Please and blocked outbound writes; expect intervening changes exactly once.
- [x] 3.2 Run scoped tests, strict OpenSpec validation and independent batch review; expect green PR CI and an explicit ship verdict.
- [x] 3.3 Verify two ordinary cuts with authentic Full, qualified artifacts and actual canaries; expect separate timing and provenance records.
- [x] 3.4 Synchronize and archive this change after acceptance; expect canonical requirements, strict validation and clean task resources.

## Delivery evidence

Implementation and independent review shipped in [PR #2786](https://github.com/Artexis10/exomem/pull/2786).
Both ordinary cuts passed authentic Full CI and qualified their original Python artifacts and five image variants.
Both deployment logs report `CANARY_ON_TARGET`.

| Release | Source | Full CI | Publisher | Canary |
|---|---|---|---|---|
| 0.178.0 | `d50d3bd8` | [43448519041](https://github.com/Artexis10/exomem/actions/runs/43448519041) | [43455668573](https://github.com/Artexis10/exomem/actions/runs/43455668573) | [43459902610](https://github.com/Artexis10/exomem/actions/runs/43459902610) |
| 0.179.0 | `be6ccf05` | [43574413193](https://github.com/Artexis10/exomem/actions/runs/43574413193) | [43577861908](https://github.com/Artexis10/exomem/actions/runs/43577861908) | [43581165273](https://github.com/Artexis10/exomem/actions/runs/43581165273) |

The second publisher qualified Python on verification attempt 2 and retained the original build attempt 1.
Unrelated [PR #2809](https://github.com/Artexis10/exomem/pull/2809) merged while its image publication continued.
