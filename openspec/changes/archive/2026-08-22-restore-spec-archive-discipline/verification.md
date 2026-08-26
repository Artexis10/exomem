# Verification record

## 2026-08-20 baseline

- Base: `origin/main` at `f6022ec018f45d61baa07fef03ab209d59a69d53`.
- Before this change: 137 active changes, 39 archived change directories, 36 canonical capability specs.
- `openspec validate --all --strict`: 173 passed, 0 failed.
- Eighty active changes have non-empty task lists with every task checked.

## Archive-set evidence

The accepted 2026-08-16 audit established the shipped backlog from code and tests rather than checkbox state. Today's recheck corroborated every one of the 80 task-complete active changes against merged delivery evidence on `origin/main`:

- 49 matched an exact OpenSpec change name in a merged PR title, branch, or body in one bounded GitHub query.
- 22 more resolved through a targeted merged-PR search using the change name.
- The remaining nine resolved through the mainline commits that updated their task artifacts alongside the implementation:
  - `stabilize-mutations-and-rich-units` → `b7a03214` / PR #299
  - `productize-hosted-marketplace-release` → `253c9aa3`, `d103f31e` / PRs #348 and #350
  - `make-records-readable-and-expandable` → `4b405f5c` / PR #457
  - `make-records-first-class-and-recoverable` → `b49ad338` / PR #452
  - `keep-remote-mcp-under-client-deadline` → `4edd81b0` / PR #219
  - `fix-graph-semantic-integrity` → `79fb1476` / PR #198
  - `complete-adoption-compile-selected` → `abd7e70b` / PR #163
  - `clear-agent-facing-friction` → `8fb365b2` / PR #285
  - `add-resource-bounded-multimodal-workers` → `cb388bbc` / PR #180

`add-sqlite-vec-backend` is an implemented prerequisite outside the 80: PR #111 merged the backend on 2026-07-03. Its sole open task is the optional 100k desk-side measurement, not product implementation, so the base contract must be synchronized before `make-sqlite-vec-opt-in` can archive honestly.

`add-setup-wizard` is another shipped prerequisite with stale unchecked boxes:
commit `9a679e4c209fed914006535e7824c079df909499` added both the implementation and
its dedicated test module on 2026-07-02. It was archived before the delivered
cognition-layer changes that modify its `guided-setup` contract.

## Disposable replay

The 80-change set was replayed in `/tmp/exomem-archive-sim.AbGSgm` from the exact base, in creation-date order with strict validation after every ten attempts.

- 68 archived mechanically.
- 12 refused without changing files.
- Every tranche and the final combined contract remained strict-valid.

The refusals were:

- Missing or unarchived base: `make-sqlite-vec-opt-in`, `allow-source-to-evidence-promotion`.
- Current canonical requirement has newer scenarios: `complete-low-interrupt-mode`, `redesign-product-command-surface`, `make-records-first-class-and-recoverable`, `surface-authored-contradictions`, `add-epistemic-inbox`, `add-one-command-onboarding`, `remote-connector-quickstart`.
- Bootstrap requirement absent pending dependency-aware ordering: `productize-cognition-layer`, `productize-pack-surface`.
- Requirement already canonical: `close-technical-memory-gaps`.

The implemented vector prerequisite itself also refuses until its `find-recall-efficiency` delta is refreshed from the current canonical scenario superset.

## Already-canonical technical-gap change

Every requirement and scenario in `close-technical-memory-gaps` exists exactly
once in its target canonical capability. Sixteen of its eighteen requirements
are byte-for-byte equivalent after whitespace normalization. The other two,
`Installed-wheel stdio product loop` and `HTTP lifecycle and timeout safety`,
are strict later evolutions: the canonical text preserves the original lifecycle
and bounded-timeout contracts while adding Records recovery and remote-ingress
coverage. The change can therefore archive with `--skip-specs` without discarding
any contract content.

`allow-source-to-evidence-promotion` is the sole owner of `Append-Only Tree
Relocation`: no active, archived, or canonical spec defines that requirement,
and commit `f50da6dd16904828288db0963fe7eec0595659b2` / PR #288 shipped the
implementation. Its delta operation was corrected from `MODIFIED` to `ADDED`
before archive; the requirement content itself was unchanged.

## Post-migration canonical inspection

- 82 delivered changes were archived in total, raising the archive from 39 to
  121 records. On the final `origin/main` base, the pre-closure tree contains
  57 active changes, including this closure record and records added to main
  after the original census.
- The canonical capability set grew from 36 to 101 specs.
- Every requirement and scenario title present in the `origin/main` canonical
  baseline remains present after migration.
- No canonical spec contains a duplicate requirement name or a duplicate
  scenario name within the same requirement. Repeated scenario names in separate
  requirements remain intentionally scoped by their parent requirement.
- `openspec validate --all --strict`: 158 passed, 0 failed.

## Closure verification

- Final integration base before self-archive: `origin/main` at
  `b82322bcd179687502fb1b120f6f8a947e5d214f`.
- `tests/test_openspec_archive_discipline.py`: 9 passed. The parser covers
  ordinary, nested, and deindented unclosed fenced examples without treating
  example checkboxes as task state.
- The archive audit reports 57 active changes and no task-complete active
  change before this record is archived.
- The GitHub lean suite on the pre-archive branch completed all 21 substantive
  CI jobs successfully, including both Python matrices, the native-NTFS held
  filesystem job, packaging, Docker, product E2E, retrieval quality and
  latency, graph convergence, OpenSpec, lint, and targeted types (run
  `32560404851`).
- Windows diagnosis established two independent mainline portability gaps.
  The branch guards POSIX-only hosted mode assertions and directory fsync;
  merged PR #745 supplies protected custody fixtures plus least-privilege DACL
  inspection. Post-rebase local verification passed 90 authorization-session,
  mutation-lock, and held-filesystem tests with 12 platform-specific skips.
- Independent review accounted for all 82 migrated records, found no lost
  baseline or archived requirement/scenario headings, no duplicate canonical
  requirement/scenario names, and no privacy leak. All 819 final canonical
  requirement blocks matched a baseline or archived source block exactly.
- Repository privacy validation passed across 3,117 files and 3,163 text
  payloads immediately before self-archive.

## Post self-archive state

- `restore-spec-archive-discipline` is preserved at
  `openspec/changes/archive/2026-08-22-restore-spec-archive-discipline`, and its
  six requirements are canonical in `openspec-record-discipline`.
- Final counts: 56 active changes, 122 archived records, and 102 canonical
  capability specs. The strict validation total remains 158 passed, 0 failed.
- The archive-debt audit reports no task-complete active change.
- Relative to the final `origin/main` base: zero baseline requirement losses,
  zero baseline scenario losses, zero duplicate requirement names, and zero
  duplicate scenario names within a requirement.
- Post-archive repository privacy validation passed across 3,118 files and
  3,164 text payloads.
