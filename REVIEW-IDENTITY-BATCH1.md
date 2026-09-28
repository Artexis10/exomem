# Review: identity batch 1 (PR #1430, `feat/identity-batch-1` @ e498a976)

**Verdict: REQUEST_CHANGES.** The branch turns one test red that passes on main. It also has one withheld = absent break in the new write-time advisory. The authority, resolve-before-create and replay logic hold up.

Reproductions: `umask 022`, `CUDA_VISIBLE_DEVICES= XDG_STATE_HOME=$(mktemp -d) uv run pytest -q -p no:cacheprovider`.

## Findings

### F1 (high): the branch turns a test red that passes on main

- **Where:** `tests/test_capture_sweep.py:567` (`test_the_copyable_instruction_blocks_have_no_room_for_the_clause`).
- **What happens:** The test pins the `docs/prominence.md` copyable blocks at `{maximal: 1484, balanced: 1495}`. The R1 wording edit moved them to `{maximal: 1500, balanced: 1494}`, so the maximal block now has zero bytes of headroom under the 1,500 B cap.
- **Reproduction:** `pytest tests/test_capture_sweep.py` gives 2 failed on the branch and 1 failed on main. The failure both share is `test_the_compact_bootstrap_budget_is_not_raised`, which hits `No module named 'tests'`. That is an import-path artifact of this container, not the branch.
- **Minimal fix:** Pick one:
  - update the pinned sizes and the docstring;
  - or, preferably, trim the maximal block so it keeps some headroom under the cap in `test_personal_baseline_contract.py`.

### F2 (medium): `entity_candidate` is computed over the owner's view, so a restricted writer sees when withheld pages exist

- **Where:** `src/exomem/capture_sweep.py`:
  - `:411`: resolution runs without the `visible` filter that `hints()` passes at `:323`;
  - `:394`/`:416`: the registry index is built from all of `corpus.pages`;
  - `:428-443`: the dependency sources are not filtered by the caller's release view.

  The egress backstop later strips withheld paths from `pages`, but the choice to emit or suppress the block was already made using the withheld pages.
- **Scenario:** A withheld page links `[[Zed Corp]]`, and no visible page does. A restricted caller (`external`, or a verified principal) writes the first visible page that links it. It receives an `entity_candidate` with `pages` holding only its own page. Its twin vault with no withheld page returns no block.
  - The caller learns that exactly one page it cannot see names this identity, and the single-page block breaks the block's own "one page becomes two" contract.
  - In reverse, a withheld linker or a withheld Entity with that title suppresses a block the twin emits.
- **Reproduction:** I used a scratch twin-vault test built on the `tests/test_derived_identifier_egress.py` helpers. The owner writes `Notes/Insights/hidden-first.md` (scope `Notes/Insights/hidden-*`, ceiling L0), then a restricted `remember` links the same name. The owner gets `pages: [hidden-first, open-second]`. Both restricted audiences get `pages: [open-second]`. The twin without the hidden page gets `null`.
- **Minimal fix:**
  - Take `visible = writer_link_visibility(vault_root)`, as `_capture_sweep_block` does.
  - Pass it into `_resolve_reference_wikilink_from_context`.
  - Filter the dependency `sources`, and the registry pages used for `resolves`/`near_matches`, through it before counting.
  - Add a twin-vault case (B, A, C × both audiences) to `test_derived_identifier_egress.py`.

### F3 (low): a vocabulary-bound create-entity on a shared name fails with a misleading error

- **Where:** `src/exomem/vocabulary_application.py:446`.
- **What happens:** `_matches` binds only `entity_type/name/summary`. When the writer returns an `identity_preparation` (it has no `path`), `_resulting_versions` raises `VOCABULARY_APPLICATION_INVALID: entity writer returned no canonical path`. That message doesn't tell the caller to decide `distinct`.
- **Status:** From reading the code; not reproduced.
- **Minimal fix:** Detect `identity_preparation` in the leaf and raise `IDENTITY_DECISION_REQUIRED` with its fingerprint, the same way `adoption_proposals.py:1266` does.

### F4 (nit): the served ceiling text drops R1's "personal vault" qualifier

- **Where:** `src/exomem/envelope.py:130`.
- **What happens:** The served `confirm_required` ends "additive entity creation follows proactive_capture" with no qualifier. The OpenSpec delta says "On a personal vault …". Hosted grants still enforce the real ceiling, so this is wording only.
- **Suggested fix:** Qualify it if the byte budget allows.

## Hypotheses cleared

- **Authority / hosted.** No file under `src/exomem/governance/`, `hosted_*`, `vocabulary_gate.py` or `vocabulary_effects.py` changed.
  - `egress._SELECTOR_ADAPTERS` still classifies `create-entity` as `mutation`.
  - The additive-authority gate classifies committed effects, not arguments, so `facets` and `identity_decision` cannot widen a grant.
  - R1 is guidance-only: `CONFIRM_REQUIRED`, prominence and scaffold. Merge, supersession and deletion stay confirm-required.
- **Create-entity entry points:**
  - MCP/REST/CLI go through `op_link`/`op_connect_memory` into `link.link`.
  - Curation and episode leaves (via curation steps) refuse a preparation at `curation.py:1190` (`CURATION_IDENTITY_DECISION_REQUIRED`), and apply re-validates the fingerprint.
  - Adoption refuses at `adoption_proposals.py:1266`.
- **Concurrent creates.** I ran 8 runs of two threads creating "Quill Works" (organization) and "quill  works" (person) through `writer_lease.invoke_command`. Every run gave 1 committed and 1 `identity_preparation`, with 0 duplicates. `identity_key` applies NFKC, casefold and whitespace collapse.
- **Replay.** The sequence was: prepare → commit `distinct` → replay the same fingerprint. The replay gives `STALE_IDENTITY_DECISION`, because the new entity joins the candidate set. The fingerprint binds the name, type, candidate ref/path/type/match, and the omitted count.
- **Egress of refusals and preparations.** `resolve_entity_candidate` applies `restricted_release_filter` before counting, so the fingerprint and candidates cover the caller's view only. `test_derived_identifier_egress.py` passes, including the create-entity alias case and the withheld-occupant path refusal. The `near_matches` field came back identical across twins (the backstop strips it).
- **Guidance.** The served contract, prominence, hook, scaffold, plugin skills and docs agree; none still calls entity creation confirm-required. The two hook copies are identical (`cmp` exit 0). `test_scaffold_no_leak.py` passes.
- **Environment failures.**
  - `tests/test_hosted_restore_candidate.py` (34) and `test_activation_lexical_term_budget` (1) fail identically on origin/main 425540b2 and on the branch: 35 failed, 21 passed on each.
- **Scoped suite on the branch.** Across 40+ envelope, egress, hosted-gateway, curation, adoption, vocabulary, link, entity, write-time, capture-sweep, memory-loop and hook modules: 1021 passed, 1 skipped, 2 failed. The 2 failures are F1 and the shared import-path artifact.
