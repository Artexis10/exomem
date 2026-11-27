## Context

`classify-sensitive-material-at-capture` added the `sensitivity-classes` registry, the `sensitivity` field and the classify operation. A class is a label on one page or row.

Governance restricts disclosure by scope. A page's ceiling is the minimum over the scopes that select it (`governance-kernel`, "Pure order-free disclosure evaluator"). The residual scope selects a page only when no other scope does ("A residual scope selects what no other scope selects"). So a new scope that selects a page can raise that page's ceiling, by taking it out of a `default_deny` residual scope.

Today's gaps:

1. A page written from classified material carries no class.
2. A page whose class is still deriving can be released. Store reads serve pending rows with a derived-behind notice (`make-vault-store-canonical` D2, D3).
3. Membership subjects (`membership.MarkdownSubject`) carry no class at all.

Every tool result passes `egress.postfilter` and `_withheld_cross_check` in `writer_lease.invoke_command`, for MCP, REST, hosted and CLI callers. Every write runs through one plan function, with a pre-plan names step that every surface calls (`one-write-pipeline` D4).

Load target (`make-vault-store-canonical` D13): 20,000 pages, about 120,000 edges, a hub with 10,000 links. A restricted first read after a commit targets about one second. Derivation runs on the store thread in slices of at most `SLICE_SECONDS` (1 s). A withheld refusal keeps the existing 55 ms floor.

## Goals / Non-Goals

Goals: classes that follow derivation, live; missing for anything not proven current; effective classes in the one egress decision; a policy selector that can only restrict.

Non-goals: audience and purpose registries, `restrict` and `unrestrict`, expiry, keeping authenticators on insist, who may clear or mask a class once a policy restricts by it, and live enablement. They belong to `restrict-disclosure-per-audience`.

## Decisions

### D1. Effective classes, stored in one column

```
inherited(P) = ∪ effective(S) for each S that P derives from
effective(P) = own(P) ∪ ((floor(P) ∪ inherited(P)) − mask(P))
```

| Set | Where it lives | Who changes it |
|---|---|---|
| own | `sensitivity` frontmatter (Phase A) | capture, classify, a plain edit of a compiled page |
| floor | `inherited_sensitivity` frontmatter, a protocol-owned key held in `page_fields.py` | the carry-over step adds; classify clears |
| mask | `sensitivity_mask` frontmatter, a pointer field with writer `classify` | classify by the owner, with a reason |
| inherited, effective | `page_sensitivity` store object | the store thread only |

`page_sensitivity(path PRIMARY KEY, effective, inherited, generation)` is a derived owner object. Every consumer reads `effective`: membership subjects, row decisions, the projection catalog and egress. A membership subject without a current `effective` value is `UNRESOLVED` for any scope with a `sensitivity` selector. That reuses the existing fail-closed `UNRESOLVED` contract of "Query-time scope membership". Page facts do not change, so `FACTS_VERSION` stays 1. The membership memo header gains the `page_sensitivity` revision.

A row's effective classes are its own classes, its collection floor, and the `effective` values of the pages its current row version names in `item_sources`.

A mask stops a class at that page: its dependents inherit `effective`, which excludes the masked class. A mask is the owner's remedy for over-restriction. Clearing a floor alone would not help while the class still arrives by inheritance.

Setting a mask and clearing a floor need the owner principal, because each one can widen access. A mask may name only a class the page holds now, by floor or inheritance, so no writer can mask a class in advance of its arrival. A mask never removes the page's own classes.

Rejected: a byte cascade that rewrites each dependent when a source gains a class. A restricted writer's out-of-ceiling side effects are skipped, so the cascade fails open for it. A raise on a 10,000-dependent hub would take about 22 minutes of projection (inferred from 7.5 files per second).

### D2. Carry-over at authored writes

An authored write calls one pre-plan carry-over step, beside the names step of `one-write-pipeline` D4. The step reads the planned page, not only the tool's arguments. It adds to the floor the effective classes of:

- every derivation ref the planned page holds after the write: its `sources` field, frontmatter fields that map to an `inherits_sensitivity` relation, and typed links with such a relation, however they arrived (`sources`, `links`, `set_fields` or body text);
- a row's source refs (`add_record` and `edit_record` gain `sources`);
- an episode's `about` refs;
- the page the write edits.

The step resolves each ref under the writer's own visibility. A ref the writer cannot read is handled exactly like a ref to no page: the write keeps it unresolved with today's warning (`note._normalize_sources`) and takes no class from it. So neither a refusal nor a class reveals that the page exists.

The plan function refuses an authored write whose floor lacks a class that the step computes. Classify and mechanical rewrites (link repair after a rename, folder merges, relocations) do not run the step and keep the floor they find. Otherwise a mechanical rewrite would freeze inherited classes into hundreds of floors, and a later clear could not flow down.

The agent decides what a write drew on, as it decides a class in Phase A. A read result that releases a page with effective classes adds one line, which names no class: if a write uses this page, name it in `sources`.

Rejected:
- A separate `drew_on` argument. `sources` and `links` already mean "drew on".
- Whole-session carry-over. One conversation often covers unrelated subjects.
- A session read record that lists shown-but-unused pages in write results. No principal resolver sets `RequestPrincipal.session_id`, and hook-injected recall arrives on another channel. So the record could not key reads to the write's session.

### D3. Live inheritance

- **Edges.** Inheritance follows `graph_edges` whose relation carries a new closed behaviour `inherits_sensitivity`. The core pack assigns it to `derived_from`, `evidenced_by`, `cites`, `refines`, `duplicates` and `supersedes`; an extension relation inherits its parent's behaviours. Episode `about` gets a graph edge with the behaviour if it has none. When stored relationships (`make-vault-store-canonical` D10) land, they are an inheritance input from that release.
- **Recompute.** A derivation that changes a page's own classes, floor, mask or one of its derivation edges marks the page's dependents pending in the same transaction. The store then recomputes them in derivation slices, walking dependents through the indexed `dst_page_key`. A cycle terminates because the walk keeps a set. A page's generation increases whenever its `effective` value changes.
- **Rows.** `item_sources` gains an index on the source ref, restricted to current row versions, so an `effective` change finds the dependent rows.
- **Caches.** Every cache that holds a scope set or a decision keys each entry on its own page's generation and the generations of its scope parents (parts, frames): `admission_memo`, membership `_MEMO`, `_SNAPSHOT_MEMO` and `_PATH_MEMO`, the store-mode membership map (`make-vault-store-canonical` D13) and the collection view decisions (D14). A global revision in a map header would drop every kept entry whenever any page's classes changed, which is the corpus-wide work D13 avoids.
- **Catalog.** Projected rows carry their page's generation. Projected retrieval under a policy with a `sensitivity` scope re-checks the sensitivity scopes against `effective` at query time, rather than trusting the scope ids stored at publish. Republish then catches up in the background.
- **Owner reads.** Inheritance work is derivation, not a write intent, so owner reads do not wait on it.
- **Version.** `page_sensitivity` takes the next owner version in `vault_store/rederive.py` (6; owner version is 5 today). Of this change and `make-vault-store-canonical` task 5.2, the second to merge rebases.
- **Pages outside the graph.** The graph indexes only Knowledge Base Markdown outside the scan-excluded folders (`epistemic_graph._is_graph_page`). A class on a page outside the graph would reach no dependent. Capture, classify and a plain edit refuse it with `SENSITIVITY_PAGE_UNTRACKED`. The release check lists already-classified pages outside the graph.

Rejected: inheritance in `page_admission`. That row's currency proof is the page's own sha256, which a source's class change does not move.

### D4. Removals flow down; floors keep what an authored write saw

Clearing a class on a source recomputes inheritance, so dependents that hold the class only by inheritance lose it. A page whose authored write came after the class was set holds the class in its floor and keeps it. This was decided on 2026-11-27.

### D5. The currency proof is one watermark

The store keeps two revisions: the last committed revision, and the revision up to which `page_sensitivity` is complete, including derivation of every committed intent. At a read's pinned snapshot:

- When the second equals the first, every page's `effective` is current. The check is O(1).
- Otherwise the store computes the pending set once per snapshot: pages with an underived intent, pages marked for inheritance work, and the dependents of both. Egress answers missing for those pages and decides the rest as usual. A page classified a moment ago is in the set until its intent derives, so it is never released under its old classes.

Projected retrieval removes the pending set before ranking, so document frequency and ranks cannot reveal a withheld page.

The check runs only when the active policy has a `sensitivity` scope and the caller is not the owner. For those callers it replaces the derived-behind notice of `make-vault-store-canonical` D2 and D3 with a missing answer, in the shape and timing of a page that does not exist. Owner reads keep D2 and D3 unchanged.

### D6. One egress decision, proved by behaviour

`egress.postfilter` and `_withheld_cross_check`, which `invoke_command` runs for every surface, decide on `effective` through membership. No per-door annotation is added.

Some facts leave without naming a path, and the postfilter cannot see them by path. Each one gets its own check, and task 4.1 lists them from the code: counts, stored rollups, aggregates, snippets, titles, link names and suggestion lists. Binary downloads decide by their Evidence page's `effective` value.

The proof is behavioural. A test builds a synthetic vault with classified canaries: a direct page, a cited page, an episode `about` page, a row with a classified source, and a binary with a classified Evidence page. It runs every registered read operation under a restricted caller and fails when any canary's path, title, text or count appears in a result. The test enumerates operations from the command registry and the hand-registered MCP resources and prompts, so a new door joins it without anyone editing the test. A count is checked by comparing results with and without the canaries, not by string match.

### D7. The `sensitivity` selector restricts only

`sensitivity` joins `_SCOPE_SELECTOR_FIELDS` in `governance/policy.py`, with three rules:

- A scope that uses it has no other selector, so its meaning stays "pages of these classes".
- `exclude` does not accept it. An exclusion by class would release pages from a restricting scope when a hub gains a class.
- A match by a `sensitivity` scope never removes the residual scope from a page. The scope can then only lower a ceiling.

An unknown key is a compile error. A vault without the store refuses a policy that uses the selector, with an error that names the store; this was decided on 2026-11-27 as temporary until those vaults run the store. An owner-only class is such a scope with `default_deny`.

A binary from before this change refuses a policy that uses the selector. A process that is already running keeps enforcing its last good policy until it restarts, then blocks the policy.

### D8. Path selectors ignore case

`membership._matches_paths` uses `fnmatchcase`, so a scope on `Health/**` misses `health/x.md`, which a case-insensitive file system treats as the same folder. Path selectors and exclusions match after casefolding both sides.

## Risks / Trade-offs

- **Spread.** Inheritance is transitive. An entity page that takes observations from many sources can collect several classes and pass them on. Task 0.1 measures the spread on a read-only copy of the owner's graph before inheritance is built; a large spread reopens D1's transitive rule. Masks are the owner's per-page remedy.
- **Granularity stays the page.** One cited figure from a health Source makes a note health. Finer granularity is out of scope.
- **Uncited paraphrase.** A write that names no source carries no class. The read-result prompt and the instructions reduce this; the re-attack lane measures it. It remains an agent-judgement risk, as classification is in Phase A.
- **Existence bits for confined writers.** A confined writer that creates, moves or links onto a path inside its writable scope can learn that a page it cannot read holds that path: `FILE_EXISTS`, `DEST_EXISTS`, `ENTITY_EXISTS`, `ARTIFACT_EXISTS`, a records key conflict, the names step's numbered suffix or a folder respelling. A folder respelling also returns the exact spelling of a withheld folder, and `FILE_EXISTS` or a numbered suffix confirms a guessed title. None reveals content or class. This change does not close them; the re-attack lane rates them, and Phase C decides before any confined writer gets a shared audience.
- **Mixed versions.** Until every process restarts on this release, an older process keeps the previous policy.
- **Derivation lag hides pages** from restricted callers during a hub raise. The owner never sees this.
- **Owner reads are not receipted.** The receipt chain writes nothing for an ungoverned owner read (`disclosure-evidence`), and this change keeps that. Non-owner reads of classified pages are governed, so they are receipted.

## Migration

None in page bytes. The store derives `page_sensitivity` on its next derivation: one walk over the derivation edges, in slices. The release check reports classified pages outside the graph.

## Proof

- Task 0.1: the spread measurement.
- The behavioural egress test from D6.
- Scoped tests for carry-over, removal, mask, the watermark and the residual rule.
- A before and after on the latency suite for a restricted first read and a hub raise, on generated data at the load target.
- The twin-world canary rig with four synthetic canaries (direct, cited, episode `about`, uncited copy), then a fresh `security-reviewer` re-attack lane on Opus. It must find no leak and no withheld-versus-missing oracle beyond the existence bits listed above. This change merges only after it passes.
