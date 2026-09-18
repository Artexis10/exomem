## 0. Preconditions

- [ ] 0.1 `activate-context-on-host-turns` is merged (both changes edit
      `working_set_runtime.py`); rebase this branch onto it.
- [ ] 0.2 Record the deterministic activation audit on the seeded corpus at the base
      commit: per-case anchor recall and precision, twin false activation, hedged-twin
      count. This is the before-image for 4.2.

## 1. Conventions registry

- [ ] 1.1 Red first: tests for shipped-equals-previous-constants (folders, tags,
      state-field order, date-field order, stopword set), override add and drop in every
      section, the five rejected folder-rule shapes, caps with findings, invalid YAML
      fallback, one bad rule not voiding the file.
- [ ] 1.2 Add `src/exomem/activation_conventions.py` on the `context_roles` load
      contract (shipped text from the scaffold, override path, digest memo, findings).
- [ ] 1.3 Ship `activation-conventions.yaml` in `src/exomem/_scaffold/_Schema/` with a
      commented override example; regenerate the plugin copy. Keep it generic:
      `tests/test_scaffold_no_leak.py` must pass.

## 2. Compiler reads the registry

- [ ] 2.1 `working_set_index`: `_page_anchor_kind` takes membership from the effective
      conventions; delete `_RAW_MATERIAL_FOLDERS` and `_SKIP_DIR_NAMES` in favour of
      `vault.in_append_only_tree` and the product's shared skip lists. Before deleting,
      diff the directories skipped today against the shared lists and report any
      difference instead of absorbing it.
- [ ] 2.2 `working_set_state`: state and date fields from the conventions.
- [ ] 2.3 `working_set_resolve`: stopwords from the conventions.
- [ ] 2.4 Conventions digest joins the index identity, the packet cache key and
      `generation` (`conventions_source`, `conventions_hash`, `conventions_findings`).
      Red first: edit the override, activate the same turn, assert a rebuilt index and a
      different hash, and that no cached packet is served.

## 3. One cue vocabulary

- [ ] 3.1 Red first: the three `context-roles` scenarios (owner's cue reaches
      `category_match`; broad cue selects a role without evidence; a cue alone abstains).
- [ ] 3.2 Add `cue_evidence` to the role model, the shipped registry (true on the eight
      roles named in the design) and the override grammar.
- [ ] 3.3 Delete `CUE_PATTERNS` and `_CUE_CATEGORIES`; derive eligible categories from
      the effective registry. Trace and update every reader of `TurnAnalysis.cues`.

## 4. Proof

- [ ] 4.1 End-to-end test on a vault with no `Products/` or `Systems/` folder, a
      non-English cue and stopword override, and a custom state field: a turn resolves,
      the packet carries the custom state, `generation` names the vault override.
- [ ] 4.2 Re-run the deterministic activation audit and compare with 0.2. Twin false
      activation and anchor precision stay inside the pre-registered bounds. If not,
      narrow the shipped `cue_evidence` set and re-run; do not reinstate a table.
- [ ] 4.3 Latency gate: `working_set` stages within `CEIL_WORKING_SET_MS` at 2k and 8k
      notes with the shipped registry and with an override at the caps.
- [ ] 4.4 Three-door parity (MCP, CLI, REST) still holds; `ask_memory` and `find`
      byte-identical for every input.

## 5. Delivery

- [ ] 5.1 Scaffold skill reference: how an agent diagnoses an abstention caused by
      uncovered conventions and proposes an override to the owner.
- [ ] 5.2 Regenerate derived artifacts (plugin tree, hosted render, capabilities doc);
      `openspec validate --all --strict`; privacy gate; full sharded corpus at the
      delivery boundary.
- [ ] 5.3 Independent review of the diff, then archive this change with
      `openspec archive` in the same delivery.
