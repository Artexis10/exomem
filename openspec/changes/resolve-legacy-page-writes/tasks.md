## 1. Red-first tests

- [x] 1.1 Add failing `tests/test_observe_memory.py` coverage: untyped page inside
      the KB, frontmatterless page, policy-protected tier (readonly/excluded),
      and the outside-KB `suggested_path` branch at the pure-function level.
      Evidence: captured red against unmodified `observe_memory.py` before
      implementation (6 failing, existing suite otherwise green).
- [x] 1.2 Add a command-layer assertion (`commands.op_observe_memory`, not only
      the module function) proving `remediation` and `resolution` survive the
      wrap into `ValueError` via `__cause__`.
- [x] 1.3 (Correction round 1) Add a command-layer test with a real page outside
      `Knowledge Base/` proving `OUTSIDE_GOVERNED_ROOT` is unchanged and its
      `resolution.suggested_path` lands under
      `Knowledge Base/Notes/Research/team/`; add command-layer assertions that
      the flattened `ValueError` string contains the exact `suggested_path`
      and `part_of` target for all three legacy-schema cases; add a
      pre-existing-collision test for `_legacy_dated_child_resolution`.
      Evidence: captured red against the pre-correction implementation (5
      failing for the right reason: `resolution is None` for the outside-KB
      case, `suggested_path`/target missing from the flattened string,
      `_legacy_routing_remediation` not yet defined, collision not avoided).

## 2. Implementation

- [x] 2.1 `observe_memory.py`: add `resolution` to `ObserveMemoryError`; attach
      remediation + resolution for the untyped-page and `FRONTMATTER_REQUIRED`
      cases; give the access-tier refusal its own, distinct remediation with no
      resolution. Keep every existing error code unchanged.
- [x] 2.2 Do not widen `_COMPILED_PAGE_TYPES`.
- [x] 2.3 Add one paragraph to
      `src/exomem/_scaffold/_Schema/references/write-scope.md`; refresh the
      scaffold skill-contract stamp (`scripts/refresh-skill-contract.py`).
- [x] 2.4 Hosted `plugins/hosted/skills/exomem/SKILL.md` sentence: NOT shipped in
      this change — see `design.md` D5 (breaks a locked release-identity
      fixture and the compatibility descriptor; regenerating those is forbidden
      to this lane). Left for a release-owned follow-up.
- [x] 2.5 (Correction round 1) Add an `OUTSIDE_GOVERNED_ROOT` branch beside
      `FRONTMATTER_REQUIRED` in the existing `except edit.EditError` handler,
      sharing one path-normalisation helper (`_legacy_page_given_form`,
      mirroring `edit._existing_page_outside_kb`'s normalisation) between both
      branches; no `edit.py` change.
- [x] 2.6 (Correction round 1) Build `remediation` from the computed
      `resolution` (`_legacy_routing_remediation`), interpolating the exact
      `suggested_path`, `part_of` target, in-place alternative, and migration
      pointer, so the flattened string an MCP agent receives carries the
      concrete next step.
- [x] 2.7 (Correction round 1) `_legacy_dated_child_resolution` de-collides
      `suggested_path` with `vault.unique_path`, matching `note.py`'s creation
      path.

## 3. Verification

- [x] 3.1 Run `tests/test_observe_memory.py` (green, 42 passed, including the 6
      new tests) and `tests/test_edit*.py` (green, no shared-fixture
      regression).
- [x] 3.2 Run `tests/test_scaffold_no_leak.py`, hosted-plugin, and
      `test_public_artifact_privacy.py` suites (green after the skill-contract
      stamp refresh; red before it, as expected).
- [x] 3.3 Run `tests/test_mcp_schema_fidelity.py` (green — no tool schema or
      description changed, so the tool-surface digest does not move).
- [x] 3.4 `uvx ruff@0.15.21 check` on changed paths (clean).
- [ ] 3.5 `openspec validate --all --strict` (CI-pinned version) — run at
      delivery.
- [ ] 3.6 `scripts/check_openspec_archive_discipline.py` — run at delivery.

## 4. Delivery

- [ ] Delivered: PR merged and change archived.
