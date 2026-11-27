## Context

`classify-sensitive-material-at-capture` added the `sensitivity-classes` registry, the `sensitivity` field and the classify operation. A class is a label on one page or row. Governance already restricts disclosure by scope: a page's ceiling is the minimum over the scopes that select it (`governance-kernel`, "Pure order-free disclosure evaluator"), so a scope that selects more pages can only lower what a reader sees.

Three gaps stop a class from restricting safely:

1. A page written from classified material carries no class.
2. A page whose class is not yet derived can be released while its derivation catches up.
3. About 110 call sites read the vault store snapshot (`vault_store/read.py` `snapshot` and `owned_page`). The snapshot filters nothing; release safety depends on each door calling egress.

The store is the canonical read path (`make-vault-store-canonical`), and every write runs through one plan function (`one-write-pipeline`). D4 of that change names a pre-plan step that every surface calls and that the plan function checks, and names `connector_boundary.require_write` in the plan function as the hook point for sensitivity governance.

Load target (unchanged, `make-vault-store-canonical` D13): 20,000 pages, about 120,000 edges, a hub with 10,000 links. A restricted first read after a commit targets about one second. A store-thread hold stays under 100 ms at p99. A withheld refusal keeps the existing 55 ms floor.

## Goals / Non-Goals

Goals: effective classes that follow derivation, live; missing for anything not proven current; one egress decision for every non-owner release; a policy selector that uses effective classes.

Non-goals: audience and purpose registries, `restrict` and `unrestrict`, expiry, keeping authenticators on insist, and live enablement. They belong to `restrict-disclosure-per-audience`.

## Decisions

### D1. Effective classes are own ∪ write-time floor ∪ live lineage

A page's effective classes are the union of three sets:

| Set | Where it lives | Who changes it |
|---|---|---|
| own | `sensitivity` frontmatter (Phase A) | capture, classify, a plain edit on a compiled page |
| floor | `inherited_sensitivity` frontmatter, a protocol-owned page field | the pre-plan step (adds only); classify (may clear, with a reason) |
| lineage | `page_lineage` store object | the store thread only |

A row's effective classes are its own classes, its collection floor (Phase A), and the effective classes of the pages its source refs name.

Rejected: a byte cascade that rewrites every dependent page when a source gains a class. Each dependent costs one byte write under the 100 ms hold bar. A restricted writer's out-of-ceiling side effects are skipped, so the cascade fails open for it. A raise on a 10,000-dependent hub would take about 22 minutes of projection (inferred from 7.5 files per second).

### D2. Carry-over at write time, through `sources`

Every write surface calls one pre-plan step, beside the names step of `one-write-pipeline` D4. It computes the floor as the union of the effective classes of:

- the pages the write names in `sources` (create and supersede already take it; this change adds it to `edit_note`, `add_observation`, `add_record` and `edit_record`);
- the page the write edits;
- the episode's `about` refs, for `save_episode`.

The step writes the union into `inherited_sensitivity` and merges the named refs into the page's `sources` field, or into a row's source refs. The plan function refuses a write whose floor holds fewer classes than the step computes. That refusal catches a surface that skipped the step.

The agent decides what a write drew on, as it decides a class in Phase A. Instructions tell it to name in `sources` every page whose content the write uses, cited in the text or not.

Rejected: a separate `drew_on` argument. It would be a second name for what `sources` already means on `create_note`, and each new argument costs tool-schema bytes.

Rejected: a whole-session carry-over, where every write takes the classes of everything the session read. One conversation often covers unrelated subjects, and this would label most writes with classes they do not hold.

### D3. Live lineage in a derived store object

`page_lineage(path PRIMARY KEY, classes, generation)` is a derived owner object in the vault store. It is not part of `page_admission`: that row's currency proof is the page's own sha256, and a source's class change leaves the dependent's bytes unchanged.

- **Edges.** Lineage follows `graph_edges` whose relation carries a new closed behaviour `inherits_sensitivity`. The core pack assigns it to `derived_from`, `evidenced_by`, `cites`, `refines`, `duplicates` and `supersedes`. These are the derivation edges the owner chose on 2026-11-01, minus the two that are not graph relations: episode `about` and record source refs. An overlay relation may take the behaviour. Path parents (parts, frames) keep their existing membership rule.
- **Recompute.** The store thread recomputes lineage in the same derivation transaction that changes a page's own classes, its floor, or one of its `inherits_sensitivity` edges. A recursive query walks the dependents through the indexed `dst_page_key`. A cycle terminates because the walk keeps a set.
- **Episodes.** A task maps `about` onto a graph edge with the behaviour if the graph does not already hold one. Until then, an episode's lineage is its floor.
- **Rows.** A row's lineage is joined at decision time from its source refs to `page_lineage`. `item_sources` gains an index on the source ref, so a page's lineage change finds the dependent rows.
- **Generation.** A page's generation increases whenever its lineage classes change. `admission_memo`, the store-mode membership map (`make-vault-store-canonical` D13) and the collection view decisions (D14) key on (sha256, generation). V4 projected item scopes republish when a generation moves.
- **Large raises.** A raise on a hub becomes chunked derivation work, each chunk under the 100 ms hold. Pending dependents answer missing to a restricted caller until their chunk commits.
- **Version.** `page_lineage` takes the next owner version in `vault_store/rederive.py` (6 today; the second of this change and `make-vault-store-canonical` task 5.2 to merge rebases). `FACTS_VERSION` stays 1.
- **Pages outside the graph.** The graph indexes only Knowledge Base Markdown outside the scan-excluded folders (`epistemic_graph._is_graph_page`). A class on a page the graph does not index would give its dependents no edges, so they would fail open. Capture, classify and a plain edit therefore refuse a class on such a page with `SENSITIVITY_PAGE_UNTRACKED`. A release check lists already-classified pages outside the graph so the owner can move or clear them.

Rejected: lineage in `page_admission`. It would need a second currency proof and `FACTS_VERSION 2` for every page.

### D4. Removals flow down; the floor keeps what a write saw

Clearing a class on a source recomputes lineage, so dependents that hold the class only through lineage lose it. A page written after the class was set keeps it, because the pre-plan step wrote it into that page's floor. Only classify lowers a floor, with a reason record that allows a revert. The owner chose this on 2026-11-27.

### D5. The currency proof and withheld equals missing

`read.py` supplies a proof; egress decides. The proof says that, at the read's pinned revision, no derivation is pending for the page or for any page in its lineage ancestry, graph catch-up included.

The check runs only when the active policy has a scope that selects by `sensitivity` and the caller is not the owner. Owner reads pay nothing.

Without the proof, egress answers missing (`make-vault-store-canonical` D2), with the response shape and timing of a page that does not exist. Inline derivation keeps the existing `UNDERIVED_CAP` bound in `vault_store/derivation.py`; beyond it, the pages answer missing.

### D6. One egress decision for every non-owner release door

A non-owner release door is any code path that returns page, row or binary content, or a fact derived from it, to a caller other than the owner. Each one reads the store through `snapshot` or `owned_page` and releases only what egress decides.

The door list starts from the egress entry points and the doors the 2026-11-27 code reading named:

- search, `recall_context`, `read_page` and section reads;
- graph context, edge views and inbound or suggested links;
- records queries, row reads, stored rollups and v4 projected retrieval;
- episodes, review queues and planning items;
- binary downloads, which decide by their Evidence page's classes;
- confined writers' preview and result echoes.

Task 4.1 completes the list from every caller of `snapshot` and `owned_page`. Each call site declares its reader as owner-only or egress-decided, with a one-line reason. A test parses the package and fails when a call site declares neither. Owner-only readers are Markdown export, backup and maintenance that serve the owner only.

### D7. The session read record is evidence, never a floor

The governance sidecar (`Knowledge Base/.governance.sqlite`) gains one table. A row holds the session id (`RequestPrincipal.session_id`), the page path, the classes released and the time.

- A read boundary commits the row before egress releases the bytes, once per top-level read, for any principal. So a crash cannot release unrecorded material.
- Rows older than 30 days are deleted at the next sidecar maintenance. The limit is a setting.
- Each write result lists the classified pages the session was shown that the write did not name in `sources`, edit, or name in `about`. The agent can then add them or leave them out on purpose.
- No class reaches a write because of a row.

The plan's B3 asked for receipts of every read that releases classified material. The receipt chain (`disclosure-evidence`) deliberately writes nothing for an ungoverned owner read, and its hash-chained append is the wrong cost for every owner recall. This record covers owner reads; governed non-owner reads keep their existing receipts.

Rejected: the table in the vault store. A read would become a store write against the 100 ms hold bar, and a store rollback would drop session state that cannot be derived again.

### D8. The `sensitivity` selector, in store mode only

`sensitivity` joins `_SCOPE_SELECTOR_FIELDS` and `exclude` in `governance/policy.py`. It matches a page or row whose effective classes intersect the listed keys. Each key must be registered; an unknown key is a compile error. The field set is closed, so an older binary refuses a policy that uses the selector: it fails closed.

A vault without the store has no `page_lineage`. Its policy compile refuses a scope that selects by `sensitivity`, with an error that names the store as the requirement. This was decided on 2026-11-27, as temporary until those vaults run the store.

An owner-only class is a scope with this selector and `default_deny`. Phase C supplies the registries and operations that write such a policy.

### D9. Two earlier leaks close here

- **Case.** `membership._matches_paths` uses `fnmatchcase`, so a scope on `Health/**` misses `health/x.md`, which a case-insensitive file system treats as the same folder. Path selectors match after casefolding both sides.
- **Create oracle.** A create whose target path holds a page the caller cannot read refuses with the answer a caller gets for a path outside its writable scopes. It never answers `FILE_EXISTS`.

## Risks / Trade-offs

- **An agent omits a source.** An uncited paraphrase carries no class. Mitigations: the write result lists the classified pages the session was shown; instructions ask for every source; the re-attack lane measures uncited paraphrase. The residual risk stays an agent-judgement risk, as classification is in Phase A.
- **Over-restriction.** A class reaches every page derived from a classified one. A note that cites a health Source for one figure becomes health. The owner lowers a floor with classify. A clear needs no owner role here; Phase C decides that.
- **Derivation lag hides pages.** A restricted caller sees pending pages as missing. The owner never does.
- **Deploy cost.** The first derivation after release computes lineage for every page: one recursive walk over 120,000 edges, chunked under the hold bar.

## Migration

None in page bytes. After release, the store derives `page_lineage` on its next derivation. Pages gain a floor on their next write. The release check reports classified pages outside the graph.

## Proof

- A twin-world canary rig run with four synthetic canaries: a classified Source, a note citing it, an episode about it, and an uncited copy. A fresh `security-reviewer` re-attack lane on Opus must find no leak and no withheld-versus-missing oracle. This change merges only after it passes.
- Scoped tests per door, plus the call-site test from D6.
- A before and after on the latency suite for a restricted first read and a hub raise, on generated data at the load target.
