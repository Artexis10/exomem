## Context

A curation plan is sealed once, approved once by its fingerprint, and executed step by step with a receipt and a witness per step. Until now each `apply` or `resume` request ran one step. An agent wrote every plan by hand, within 64 steps and 256 KiB. This change adds a server derivation for the commonest large plan, a rehome or rename of many pages, and makes the lane carry it. The design follows "Writing effective tools for agents" (Anthropic, 2025): one operation per intent, and a result sized for the reader rather than a dump of internal state.

## Decisions

### D1. One sealed plan per relocation, never split

The derivation seals one plan for the whole set. Two plans sealed side by side go stale: the first plan's `repoint-links` step rewrites pages that the second plan moves, so the second plan's bindings no longer match. The size bounds (D4) decide how many pages one call may name.

### D2. Derivation reads registries, not code lists

- The fields that hold project keys come from the `page-fields` registry (`carries: project-key`), in pack order.
- A compiled page's destination comes from `note.planned_destination`, with the project registry as the plan leaves it, so a new key places its pages as it will.
- The filename follows the page's title under the vault's filename style. A collision is numbered as a typed write numbers it. A stem that another live page holds as its title is a collision too (`title_occupancy.TitleCheck`, excluding the page that moves), so a renamed page never takes over that page's bare links.
- A page without a typed folder, such as an entity, stays in its folder and takes its title-named path there.
- A field that the registry gives `shape: list` gets a list when it gains its first key; any other field gets a single value.
- When the leaf refuses one page's step at preparation, that page is listed under `refused` and preparation runs again without it, at most three times. When it refuses a Source's correction, the Source is listed under `refused`, keeps its keys, and the pages that cite it still relocate.
- The memory refs among the named pages resolve over one identity census.

### D3. Sources keep a key that another citer still needs

Each relocated page's cited Sources come from the Source closure. Each Source's citers are its inbound links, read once for every Source and for each Source's earlier paths (`vault.lineage_link_targets`), plus the pages its closure back-references name. A Source gains the destination key. It loses the replaced key only when no citer outside the relocated set carries that key. An unreadable citer counts as carrying every key, so a read failure keeps a key rather than dropping it.

### D4. Size: one read bound, a derived step cap, no unused preimages

- `MAX_SEALED_BYTES` (1 MiB, the read bound that already existed) is checked when the store writes a plan, so a plan that seals always loads again.
- A sealed `derived_by` field lifts the step cap to 2048 for a server derivation only. In practice the byte bound binds first: an 81-step relocation sealed at 180,399 bytes, about 2.2 KB per step.
- Move, pointer edit, repoint, delete and recover steps seal no full-text preimage. Their undo reads the sealed line delta, the pointer-write record or the trash identity.

### D5. Execution: several steps per request, each at its own seams

`apply`, `resume` and `apply-compensation` take the run claim and record the approval as `scope-curation-holds-to-canonical-seams` defines. `_advance` then runs the steps one at a time. Each step holds the vault mutation boundary only at its own canonical seams, or whole when its leaf does not guard itself, so per-step crash safety, receipts and witnesses are those of a single step. `_advance` adds no hold of its own. Each step's derived fan-out runs when the step ends (`fanout_per_commit`), unless the request already holds the boundary, as the episode workflow and a revert do. There every step joins that one hold, and the fan-out runs once when the hold ends, as it did before this change: draining it per step would only lengthen the hold. The corpus and freshness registries learn of a write only from that fan-out, so the next step binds the bytes that the step before it wrote. The request's terminal still reports what those fan-outs reported. Between steps `_advance` lets a waiting foreground write take the boundary. It stops starting steps when the rest of the request budget would not cover the slowest step so far plus the delivery reserve, and answers `executing` with `progress`.

A refusal of the request's first step raises, exactly as with one step per request. After committed steps, no refusal raises, because those steps are durable and a raised error would tell the caller that nothing happened. The request answers `stopped` beside its progress. `CURATION_RUN_BUSY` and `MUTATION_BUSY` keep the phase `executing` with `next_action` `resume`, because another writer held the run or the vault only for a moment. Any other refusal answers the run's own `partial` or `blocked` phase, or `partial` with `resume` when the refused step left no receipt. The curate skill follows `next_action`. `stopped.committed` is the settle's answer for `CURATION_RUN_BUSY`. Otherwise it is false when the request recorded the stopped step as refused, and null when it cannot tell, as after a refused settle hold. The next resume settles a null step from its witness.

Rejected: a background maintenance job per relocation. The step loop already keeps every request inside its budget, and one job per vault would block other maintenance while a long run waits for resumes.

### D6. A derived plan previews as a summary

The relocation reply, `preview` and `propose-compensation` of a derived plan give counts and at most 20 sample rows per group. `apply` and `resume` of a derived run give counts, not every receipt. `status` still reads the whole run. A derived preview carries no ownership question, because the request named the project. The plan seals the relocation's summary (`approval_summary`), so a later `preview` shows the same groups the person approved, besides the counts it derives from the steps.

### D7. The link rewrite splits by referring page

One `repoint-links` step holds the vault mutation boundary for its whole batch. At thousands of pages a single step ran past the request budget. A relocation whose referring pages exceed `REFERRERS_PER_STEP` now ends with several `repoint-links` steps. Each names its `referrers`, and no page is named by two steps, so a page's rewrite never spans two holds and each step's undo returns only its own pages. The split is by referring page, never by moved page: a page that links several moved pages is rewritten once. Each chunk binds its pages at the bytes that the plan's earlier steps leave (`chained`), as the single step did. Each chunk seals only the moves and path renames that its own pages use. A chunk that sealed every move made the sealed plan grow with moves times chunks, and a relocation of 100 pages in a 3,000-page vault reached 982 KB of the 1 MiB bound. The narrowed lists rewrite each page to the same bytes as the full lists, and a chunk whose pages would differ keeps the full lists.

The guard's recheck also cost time per rename: each rename re-derived the batch's allowed census names for every guarded directory, which made a step quadratic in its renames. The allowed names are now found once per directory and batch (`_CensusAllowance`). The guard keeps its check: every rename still re-reads each guarded directory's census and compares it with the expected census plus the batch's allowed changes.

### D8. A proposal prepares under the request budget

A relocation once prepared all its steps in one call, so a large batch could outlast the request budget and keep nothing. `relocate` now prepares under the budget by the rule of `_advance`. It stops before a step, or before the seal, when the rest of the budget would not cover the slowest step so far plus the delivery reserve. Each call prepares at least one step. A call that stops keeps what it prepared and answers `preparing` with `progress` (steps, prepared, this call) and `next_action: relocate`. The same call made again goes on from there and seals once every step is prepared, so one approval covers the whole plan.

`curation.Preparation` holds each prepared step's digest, its binding item and the bytes that later steps read. A pass keeps a step only while that step and every step before it open the new plan unchanged, under the same registry identities. A pass after a halving or a left-out page keeps the shared prefix by the same rule. A step's item depends only on the vault, the registries and the steps before it, so a kept step is the one a fresh preparation would make. A duplicate fold and an inverse move leave state outside their item, so neither is kept. The seal rechecks every binding against the vault. A page that changed after its step was kept refuses the seal with `CURATION_BINDING_STALE`, and the refusal drops what was kept.

The kept preparation waits in the vault's state directory under `relocate-preparations/`. Its name is a digest of the call's arguments and of who asks: the audience, the authorization session, the purpose and the connector ceiling (`connector_boundary.admission_identity`). Each of them decides which pages and referrers the caller sees and writes, so two owner clients under different ceilings never share a preparation. The ceiling's denied scopes also fix its writable scopes, because the host configuration gives each writable scope to one client. A caller whose ceiling cannot be read keeps nothing and prepares in one call. No run exists before the seal, so the run store cannot hold it. It is a cache of work that the same call can redo: it expires after 15 minutes, a read that finds it expired deletes it, and it is not vault content. The continuation is the same `relocate` call, not `resume`. `resume` belongs to the execution policy class, so a connection allowed only to propose could not finish, and each continuation would ask for a destructive confirmation.

Rejected: a partial plan sealed per call and joined at the end. Each part would need its own approval or a join that rechecks every part, and the person approves one plan.

## Measurements

Measured on 2026-11-22 on a laptop, with a 130-page test vault and the corpus cache on. The proof test `tests/test_curation_relocate.py` builds the same vault.

- Propose of an 81-step relocation (40 pages, 40 Sources): 2.4 to 2.9 s.
- One step: about 1.4 to 1.9 s, mostly the move leaf's commit (0.75 to 0.96 s).
- Corpus build per step: about 4 ms with the cache on. A corpus pinned per proposal would save nothing, so none was built.
- Binding recheck of every uncommitted step: about 28 ms per step at 75 uncommitted steps, 0.4 ms per binding. The 1 MiB bound caps a plan at about 470 steps, so the recheck stays under 0.2 s per step. It stays exact rather than sampled.
- A 60 s request budget ran the 81 steps in three calls (31, 46 and 4 steps).
- A foreground write that waits during a step commits before the next step starts.

Measured on 2026-11-23 on the same laptop with a generated vault: 3,000 research notes in one project folder, each linking five others at random. The relocation moves 100 of them to a new project, then the bench applies its undo. The request budget is 50 s.

| Measure | Corpus cache on (the service default) | Corpus cache off |
|---|---|---|
| Plan | 119 steps: 100 moves and 19 link rewrites of at most 25 referring pages, 451 in all | the same |
| Sealed plan | 581 KB with each step's moves narrowed; 982 KB before | 982 KB, before narrowing |
| Propose | 22.2 s, longest hold 0.24 s | 191.8 s |
| Apply | 4 calls (43.1, 43.4, 43.2 and 30.9 s), longest hold 3.0 s | 10 calls, longest 43.5 s, longest hold 8.8 s |
| Propose the undo | 22.3 s, longest hold 0.31 s | not timed |
| Apply the undo | 4 calls, longest 44.6 s, longest hold 2.6 s | 9 calls, longest 47.9 s, longest hold 8.3 s |

Before the split and the recheck fix, with the cache off, a 300-page vault with 10 moved pages and 50 referring pages held the vault for 65.8 s in its one link rewrite and ran 82.6 s in one call. The recheck fix alone brought that hold to 6.6 s.

With the corpus cache off, each move's preflight builds the corpus again, so the proposal grows with pages times moves. The service runs with the cache on. The test suite turns it off by default.

## Risks

- A long run now spans several requests without new approvals. The approval still binds the exact plan bytes, and every step rechecks its bindings.
- An in-place pointer edit on a writable page no longer stamps `updated`. Where a page belongs is not a content change, and the edit is reversible from its sealed delta.
