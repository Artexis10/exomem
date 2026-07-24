# Tasks: add-cross-domain-bridges

## 1. Bridge schema + release grant

- [ ] 1.1 Red test `tests/test_governance_bridges.py`: `bridge_of`/`bridge_scope`/
      `bridge_review` frontmatter parses; a `kind: release` grant (with
      `content_hash`) parses; `simulate` shows the bridge decision.
- [ ] 1.2 Extend `governance/policy.py` (release-grant kind) and
      `governance/decisions.py` (a bridge note releases per its own scope even
      when `bridge_of` targets are restricted).

## 2. Hash-bound release at op_get

- [ ] 2.1 Red test: an approved, unchanged bridge releases; an edited bridge
      withholds with `RELEASE_STALE`.
- [ ] 2.2 In `op_get`/`annotate_page`, compare `get_page`'s computed content hash
      to the grant hash (zero extra IO); mismatch → withhold notice.

## 3. Provenance stripping

- [ ] 3.1 Red test: released bridge omits frontmatter `sources`/`evidence`,
      history, and `## Relations` edges into restricted scopes; hit-level
      `graph.seed`/`relation_match`/`superseded_by` naming restricted paths
      stripped.
- [ ] 3.2 Implement provenance strip in `annotate_page` (get) and `annotate_hits`
      (find) for bridge releases; wire L2 constraint strings through the projector.

## 4. Lifecycle + re-review

- [ ] 4.1 Red test: `bridge_review` due → review-queue item; restricting/deleting
      a `bridge_of` source → dependent bridge flagged; a local-model draft is not
      released until owner-approved.
- [ ] 4.2 Wire `bridge_review` into the review/Inbox queue; source-change flagging;
      full propose→approve→re-approve lifecycle end-to-end (two audiences).

## 5. Gates

- [ ] 5.1 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q
      tests/test_governance_bridges.py tests/test_governance_egress.py
      tests/test_govern_memory_tool.py` green.
- [ ] 5.2 `uv run python -m pytest tests/test_latency_gate.py -q` green.
- [ ] 5.3 `uvx ruff check` clean; `openspec validate add-cross-domain-bridges
      --strict` green.
