## 0. Preconditions

- [x] 0.1 `activate-context-on-host-turns` (169e6deb) and `make-anchor-resolution-sound`
      (6cb28cfa) are merged; this branch carries main.
- [x] 0.2 The tokeniser fix (`make-anchor-resolution-sound` tasks 4a) is merged (c5712f02)
      and this branch carries it.
- [x] 0.3 Record the deterministic activation audit on the seeded corpus at the base
      commit: per-case anchor recall and precision, twin false activation, hedged-twin
      count. This is the before-image for 5.2.

## 1. Conventions registry

- [x] 1.1 Red first: shipped-equals-previous-constants (folders, tags, skip folders,
      state-field order, date-field order, stopword set, rare-term threshold); override
      add and drop where dropping is allowed; `stopwords.drop` and skip-folder drop
      ignored with a finding; the rejected folder-rule shapes (absolute, `..`, leading
      `.` or `_`, knowledge-base-prefixed, entity folder, a Planning or Records tree,
      append-only tree); case-insensitive segment matching, pinned on a lowercase
      `products/` folder; caps with findings; the 256 KiB refusal before parsing; invalid
      YAML fallback; one bad rule not voiding the file; threshold range 1 to 3 and
      unknown `resolution` keys; the digest taken over effective values, so two files
      that resolve to the same conventions share a digest.
- [x] 1.2 Add `src/exomem/activation_conventions.py` on the `context_roles` load
      contract (shipped text from the scaffold, override path, digest memo, findings,
      size cap before parse).
- [x] 1.3 Ship `activation-conventions.yaml` in `src/exomem/_scaffold/_Schema/` with a
      commented override example that includes template headings under `stopwords.add`; regenerate the plugin copy. Keep it generic:
      `tests/test_scaffold_no_leak.py` must pass.

## 2. Compiler reads the registry

- [x] 2.1 `working_set_index`: `_page_anchor_kind` takes membership from the effective
      conventions and never evaluates a page the Planning or Records passes admitted;
      the walk skips `anchors.skip_folders`; `_RAW_MATERIAL_FOLDERS` gives way to
      `vault.in_append_only_tree` plus the governance trees; `_archive` stays walked.
      Red first: a staged upload and a template tagged `hub` are not anchors; an
      archived entity stays an anchor; a `Planning` folder rule is refused.
- [x] 2.2 `_categories` consults `semantic_language_registry.resolve_category` first,
      taking its result only when the status is not `unregistered`, and the built-in map
      second. Red first: a category alias added in the vault's semantic-language override
      earns its category; "Next Steps" still maps to `action`; no category earned on the
      scaffold vault or the audit corpus is lost.
- [x] 2.3 `working_set_state`: state and date fields from the conventions.
- [x] 2.4 Stopwords and `rare_term_max_anchors` from the conventions, read once per build
      and passed to both users (turn matching and `rare_term` in `working_set_resolve`;
      derived short-name admission in `working_set_index`). The resolver enforces a floor
      of 2 on `working_set_lexical_min_terms`.
- [x] 2.5 The conventions digest is stored in the sidecar `meta` table; a mismatch wipes
      the sidecar like a `SCHEMA_VERSION` mismatch; bump `SCHEMA_VERSION`. The digest
      joins the packet cache key, `generation` (`conventions_source`,
      `conventions_hash`, `conventions_findings`) and the continuity payload. Red first:
      add a stopword with no vault write and assert a rebuilt sidecar, a derived alias
      gone, no cached packet served and an older token reported `stale`.

## 3. One cue vocabulary

- [x] 3.1 Red first, over the audit corpus and an adversarial turn set that includes
      `?`, `how much is left`, `what about`, `what is the budget`, `what is next` and
      `we already decided`: the `context-roles` scenarios; a no-new-category test; a
      no-new-trigger test (no anchor becomes `resolved` on a turn for which the deleted
      table made no category eligible); and a named-difference fixture asserting exactly
      the question-mark loss.
- [x] 3.2 Add `evidence_cues` and `evidence_categories` to the role model, the shipped
      registry (the eight roles, with the deleted table's patterns and sets; `what about`
      added to `open_questions`) and the override grammar (both keys join
      `_OVERRIDE_FIELDS` and extend by union); a cue is evidence only when it is in the
      effective `evidence_cues` and passes the bounds (three characters, at least one
      term, whole terms in order), otherwise a finding; an `evidence_cues` entry also
      selects its role; categories validated through
      `resolve_category`; add the roles caps and the size cap before parse.
- [x] 3.3 Delete `CUE_PATTERNS` and `_CUE_CATEGORIES`. Eligible categories are computed
      in the runtime from the effective registry and passed into `candidates_for`;
      `analyze_turn` stays registry-free and `TurnAnalysis` drops `cues`. Update every
      reader and the tests that read `cues`.

## 4. Governed write path

- [x] 4.1 Red first, through the tool entry point and never `Path.write_text`:
      `schema_memory` subjects `context-roles` and `activation-conventions` validate a
      proposal and return findings, diff it, save through `save-roles` and
      `save-conventions` with `proposal`, `why` and `expected_hash`, refuse a stale hash,
      refuse a proposal with any finding, refuse the generic `save` flag and `infer`,
      and write only the override file.
- [x] 4.2 Implement on the `save-relations` pattern; CLI and REST parity.
- [x] 4.3 The hosted gateway allows the two subjects while `manage_memory_file` and
      `edit_memory` stay refused for the schema folder. Red first on both halves.

## 5. Proof

- [x] 5.1 End-to-end through the tools on a vault with no `Products/` or `Systems/`
      folder: an agent saves a conventions override (custom resource folder, a custom
      state field, added stopwords) and a roles override (a non-English cue with an
      evidence category); a turn resolves, the packet carries the custom state, and
      `generation` names both vault registries.
- [ ] 5.2 Re-run the deterministic activation audit and compare with 0.3. Recall,
      precision and twin false activation stay inside the pre-registered bounds. If a
      role drifts, shrink its shipped `evidence_categories` and re-run; do not reinstate
      a table.
- [x] 5.3 Latency gate: `working_set` stages within `CEIL_WORKING_SET_MS` at 2k and 8k
      notes with the shipped registry, with an override at the caps, and with one rule
      admitting a large folder (reported, not gated).
- [ ] 5.4 Three-door parity (MCP, CLI, REST) still holds; `ask_memory` and `find`
      byte-identical for every input.
- [ ] 5.5 Real-turn run on the owner's snapshot with the shipped registry: every
      negative turn still abstains and nothing is served from a partial anchor. Evidence
      to the owner's knowledge base, not the repository.

## 6. Delivery

- [ ] 6.1 Scaffold skill reference: how an agent reads `generation`, diagnoses an
      abstention caused by uncovered conventions, validates and saves an override with
      the owner's approval.
- [ ] 6.2 Regenerate derived artifacts (tool schemas and fingerprint, capabilities doc,
      plugin tree, hosted render, harness modules pin); `openspec validate --all
      --strict`; privacy gate; full sharded corpus at the delivery boundary.
- [ ] 6.3 Independent review of the diff, then archive this change with
      `openspec archive` in the same delivery.
