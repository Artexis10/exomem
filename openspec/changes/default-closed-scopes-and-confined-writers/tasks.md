## 1. Residual scope

- [x] 1.1 Add `residual` to the scope document fields, `Scope` and `_parse_scope` in `governance/policy.py`; refuse selectors or excludes on a residual document, a second residual scope and a non-boolean value; expose `Policy.residual_scope_id`.
- [x] 1.2 Carry `residual` through `protective_scope_documents` and `compile_protective_scopes`, so the pinned requirement covers it.
- [x] 1.3 Add one membership completion helper in `governance/membership.py` and apply it in `evaluate`, `evaluate_snapshot`, `prospective_scopes`, the classified returns of `evaluate_path_only` and `evaluate_metadata`; keep a raw variant for `_evaluate_markdown_scopes`; never assign the residual scope to a directory.
- [x] 1.4 Add `vocabulary/instances.public_storage(root, path)` and treat public vocabulary storage as classified with no scope.

## 2. Confined writers

- [x] 2.1 Accept the optional client key `writable_scope_ids` in `connector_boundary._read_snapshot`, with the exclusivity checks and the residual-in-default-denied check.
- [x] 2.2 Add `write_ceiling(root, who)` and the inbox resolver; define the restricted writer in one place.
- [x] 2.3 Rewrite `require_create` and `require_write` around the write ceiling; decide before any existence check; one `WRITE_REFUSED: target is unavailable` text for path confinement; apply the same ceiling to current and proposed canonical subjects of structured store mutations.
- [x] 2.4 Replace the capture visibility invariant with "a capture page's membership equals its path's membership" in `require_write` and `verify_capture_namespaces`.
- [x] 2.5 Wire the move source and destination (`move_file.py`) and the delete source (`delete_file.py`) through the gate; confirm the batch path and every existing `require_create` caller reach it.

- [x] 2.6 Skip a restricted writer's side effects outside its write ceiling (activity log and index entries, `ingested_into` back-references, link rewrites); replace the log and index ride-along and the visible-linker move refusal; warn when a back-reference is skipped; a Records transition keeps its required audit entry and is refused instead.
- [x] 2.7 Write curation runs, episode journals and relation review records under `_Governance/curation/` and `_Schema/relation-reviews/` for a restricted writer; keep policy documents and the activation manifest refused.
- [x] 2.8 Let the vocabulary authority decide a restricted caller's vocabulary mutation before the write gate, and admit a granted change to the registry storage it names.
- [x] 2.9 Redirect a restricted writer's generated destination into the capture path under the same top-level tree (Source capture, episode recap, note), keeping the folders below the tree; refuse when none admits the write; never redirect an explicit destination.

## 3. Source and recap destination

- [x] 3.1 Place a restricted writer's Source capture under its inbox in `add.admit_source_destination`; refuse when it has no inbox.
- [x] 3.2 Add one recap-folder helper and use it in `episode_memory`, `working_set`, `working_set_heat`, `context_catalogue`, `dreamer_families` and `move_file`.

## 4. Proof

- [x] 4.1 Update the tests that pin the old behaviour: the occupied-destination cases in `tests/test_derived_identifier_egress.py`, the write-down, capture and move cases in `tests/test_connector_boundary.py`, the invariant in `tests/test_connector_boundary_arming.py`, and the cross-audience recap case in `tests/test_episode_memory_command.py`.
- [x] 4.2 Add a twin-world test: a withheld page (residual and another matter) against an absent one, for a fenced connector and a client-bound non-owner, across search, recent context, read, root browse and every write door; responses are byte-identical; the connector sees its own matter page and its own inbox recap.
- [x] 4.3 Add a write-door matrix parametrised on writer kind and target class, with one representative door per write kind.
- [x] 4.4 Add the episode case: an unfenced recap is never served to a fenced connector; a fenced connector's recap lands in its inbox and only it and the owner receive it.
- [x] 4.5 Add one configuration refusal case: an exclusivity breach, a residual scope missing from the default denied set, and writable scopes granted with no residual scope declared.
- [x] 4.6 Document the residual scope, inbox scopes and `writable_scope_ids` in the scaffold skill, in generic wording.
- [x] 4.9 Prove 2.6 to 2.9: a fenced client cites a readable Source it may not write; inbox creation leaves the log and top index unchanged; a fenced episode prepares, records and resumes; a session's vocabulary change is refused with a request id, then admitted after the owner's grant; a fenced `remember` lands in the Notes-tree inbox; a fenced Source capture under the Sources-tree inbox can be cited in `sources:`.
- [ ] 4.7 Run the scoped governance suites, the governance overhead gate, `openspec validate --all --strict`, the privacy gate and the full suite.
- [ ] 4.8 Re-run the 2026-11-01 audit rig with a residual scope and writable scopes; record that derived notes, owner recaps and write probes no longer reach another context.
