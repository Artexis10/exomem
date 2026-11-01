## 1. Residual scope

- [ ] 1.1 Add `residual` to the scope document fields, `Scope` and `_parse_scope` in `governance/policy.py`; refuse selectors or excludes on a residual document, a second residual scope and a non-boolean value; expose `Policy.residual_scope_id`.
- [ ] 1.2 Carry `residual` through `protective_scope_documents` and `compile_protective_scopes`, so the pinned requirement covers it.
- [ ] 1.3 Add one membership completion helper in `governance/membership.py` and apply it in `evaluate`, `evaluate_snapshot`, `prospective_scopes`, the classified returns of `evaluate_path_only` and `evaluate_metadata`; keep a raw variant for `_evaluate_markdown_scopes`; never assign the residual scope to a directory.
- [ ] 1.4 Add `vocabulary/instances.public_storage(root, path)` and treat public vocabulary storage as classified with no scope.

## 2. Confined writers

- [ ] 2.1 Accept the optional client key `writable_scope_ids` in `connector_boundary._read_snapshot`, with the exclusivity checks and the residual-in-default-denied check.
- [ ] 2.2 Add `write_ceiling(root, who)` and the inbox resolver; define the restricted writer in one place.
- [ ] 2.3 Rewrite `require_create` and `require_write` around the write ceiling; decide before any existence check; one `WRITE_REFUSED: target is unavailable` text.
- [ ] 2.4 Replace the capture visibility invariant with "a capture page's membership equals its path's membership" in `require_write` and `verify_capture_namespaces`.
- [ ] 2.5 Wire the move source and destination (`move_file.py`) and the delete source (`delete_file.py`) through the gate; confirm the batch path and every existing `require_create` caller reach it.

## 3. Source and recap destination

- [ ] 3.1 Place a restricted writer's Source capture under its inbox in `add.admit_source_destination`; refuse when it has no inbox.
- [ ] 3.2 Add one recap-folder helper and use it in `episode_memory`, `working_set`, `working_set_heat`, `context_catalogue`, `dreamer_families` and `move_file`.

## 4. Proof

- [ ] 4.1 Update the tests that pin the old behaviour: the occupied-destination cases in `tests/test_derived_identifier_egress.py`, the write-down, capture and move cases in `tests/test_connector_boundary.py`, the invariant in `tests/test_connector_boundary_arming.py`, and the cross-audience recap case in `tests/test_episode_memory_command.py`.
- [ ] 4.2 Add a twin-world test: a withheld page (residual and another matter) against an absent one, for a fenced connector and a client-bound non-owner, across search, recent context, read, root browse and every write door; responses are byte-identical; the connector sees its own matter page and its own inbox recap.
- [ ] 4.3 Add a write-door matrix parametrised on writer kind and target class, with one representative door per write kind.
- [ ] 4.4 Add the episode case: an unfenced recap is never served to a fenced connector; a fenced connector's recap lands in its inbox and only it and the owner receive it.
- [ ] 4.5 Add one configuration refusal case: an exclusivity breach and a residual scope missing from the default denied set.
- [ ] 4.6 Document the residual scope, inbox scopes and `writable_scope_ids` in the scaffold skill, in generic wording.
- [ ] 4.7 Run the scoped governance suites, the governance overhead gate, `openspec validate --all --strict`, the privacy gate and the full suite.
- [ ] 4.8 Re-run the 2026-11-01 audit rig with a residual scope and writable scopes; record that derived notes, owner recaps and write probes no longer reach another context.
