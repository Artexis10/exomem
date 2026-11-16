# Design

## Context

A deep dive in a real store spans two to six compiled research notes. The notes are joined by wikilinks or typed relations, and they cite Sources and episode captures. A sample of eight researched subjects in one personal store had these shapes: a hub with four sister notes, a numbered series of layers, a primary note with a later re-analysis, and engineering investigations whose working record lives in episode Sources. Open questions appear under `## Open questions`, `## Open threads` or similar headings. External references often sit in a body section rather than in the `sources` field.

The existing pieces each cover a part of this:
- The derived views (`exomem://map`, `exomem://profile/...`) supply ref parsing, signed pagination and the terminal egress guard.
- The stance block supplies an input-digest cache.
- The kinds registry already declares `open_question` (trait `question`) and the assertion-trait kinds. The relations registry declares the closing relations, and the traversal-profile registry declares which relation families a walk follows.
- `served_context` is a per-principal sidecar with bounded retention.

## Goals and non-goals

Goals:
- One call that returns the whole compiled body of a subject.
- The same members on every read of the same seed.
- Visible gaps instead of silent truncation.
- No recompile while the inputs are unchanged.
- Change tracking per principal.

Non-goals:
- Server-side prose synthesis. The server makes no model calls; the client writes the prose from the brief.
- Activation changes.
- Changes to topic profiles.
- A persisted brief note type.

## Decisions

### D1. A brief is keyed by a seed page, not by a domain

A registry domain is much wider than one researched subject. A seed page fixes the subject and gives the watermark a stable key. Topic text resolves to the top compiled-note hit of the existing hybrid search. Up to ten other compiled-note hits that are not members come back as `related`, so a caller who meant another subject can switch with one read.

### D2. Members come from the seed alone

The walk follows the relation families of the shipped traversal profile `brief`, up to two hops. It walks only through compiled notes, so an entity or a domain page with thousands of links cannot pull the whole store in. Cited Sources attach as leaves. Search hits never add members, so a read of `exomem://brief/<id>` serves exactly the members that the minting read served. Members are ordered by distance and then by recency, with caps of 40 notes and 40 Sources. A capped candidate is counted in `missing`.

The relation families live in `traversal-profiles.yaml`. The profile takes the plain-link family and the provenance families, and a vault can extend it in its overlay. Code holds no relation list (C4).

### D3. Sections come from closed traits

- `conclusions` takes the units whose kind carries the `assertion` trait, plus categorised observations.
- `open_questions` takes the units whose kind carries the `question` trait and that are not closed. The closing logic is the existing `answers`, `resolves` and `bears_on` handling of the question audit.
- `sources` takes the Source members and the external links that the Markdown link parser finds in member bodies.

Headings reach a kind only through the kinds registry. The shipped pack gains the aliases `open_threads` and `open_thread` for `open_question`, and a vault adds other spellings or languages in its overlay. No code matches words (C6).

### D4. Cache under an input digest (the stance pattern)

The brief cache is an in-memory LRU of 128 entries under a lock. Its key holds:
- the root and the caller binding;
- a digest of each member's ref, content hash, lifecycle and path;
- a digest of the closing relations that target member units;
- the registry digest and the limits.

The member walk reads content hashes from the index, so a cache hit reads no page.

The walk itself is kept for the owner, in a second LRU of 128 entries that holds the walk and its closing edges. Its key is the root, the caller binding, the seed path, the graph snapshot's token, the recall policy identity and the registry digest. The token is `(schema_version, extension_registry_hash, generation, instance)`, read in the walk's own read transaction. Every write of graph rows bumps `generation` in the transaction that writes them, and a full rebuild replaces `instance`, so any edit, however small, ends the entry. An owner's warm read of an unchanged graph reads no edge. A restricted caller's release decision reads governance policy, tombstones and authorization that no graph token names, so its walk is made again on every read.

A recorded precedent declined a build cache for topic profiles, because no key could cover every input that the build reads. The brief's inputs are closed:
- The member texts are covered by their hashes.
- The closure of an open question can come from a page outside the member set, so its closing relations are in the key.
- Visibility is covered by the caller binding, and the terminal guard re-decides every served item at egress.

### D5. A watermark per principal in a private sidecar

A sqlite sidecar in the state root, modelled on `served_context`, holds one row per principal audience and brief seed. A row stores member content hashes, unit hashes and `read_at`. It advances when the last page is served, so a client that reads one page of a long brief does not lose the rest of the diff. Bounds: 10,000 rows, least-recently-read eviction, 400-day retention. A user returns to a subject after months, so the retention is longer than that of `served_context`. The sidecar is private metadata that only the owner can read and never usage telemetry. The diff is computed after the caller's view filter, so a withheld member never appears as removed or new.

### D6. Surface

`read_page(part="brief")` maps to a new optional boolean `brief` on `read_memory`, under the surface-parity contract that `units` already follows. The response is a derived view `exomem://brief/<seed id>`, with the existing continuation, stale-generation refusal and terminal egress guard, through a new `brief` branch in `guard_view_response`. No public tool is added. The scaffold operations reference teaches the call.

## Load target and latency budget

The product load target is 20,000 pages, about 100,000 units, 120,000 edges and a 10,000-link hub (from the `make-vault-store-canonical` design). At that size, brief reads at the member caps have these budgets:
- **Cold compile** (a cache miss: up to 80 member reads and unit parses): p95 under 1000 ms, the activation bar.
  A 10,000-link hub as the seed is the worst single shape, not a p95 sample. It is recorded with its measured range and kept near this bar.
- **Warm read for the owner** (memo hit, digest, cache hit, watermark diff): p95 under 250 ms.
- **Warm read for a restricted caller** (walk, digest, cache hit, watermark diff): p95 under the cold bar, 1000 ms. Its release filter reads state that no graph token names, so its walk is not memoized. The map views exclude restricted builds from their cache for the same reason.
- **Topic text** adds one hybrid search.

Per-request work is bounded by the member caps and the page size. The walk passes only through compiled notes, so a 10,000-link hub cannot widen it. Proof: cold and warm timings on a generated store at the load target, plus a read-count test showing that a warm read reads zero pages. Production latency and errors are recorded under the route label `read_page:brief`.

## Evaluation

The brief is additive and read-only, so it ships first. An evaluation then decides whether the client instructions route "return to a subject" requests to it.
- **Cases:** eight researched subjects from one personal store, compared case by case. Arm A is today's recall and synthesis; arm B reads the brief and then synthesises.
- **Actor:** the shipped Claude Code client, two repeats.
- **Keys:** an independent lane writes them from the store's pages.
- **Judge:** a blind judge from another model family, shown the same reference view for each arm.
- **Metrics:** member recall, coverage of conclusions, open questions and sources, faithfulness, wall time and tokens, each with a confidence interval. An interval that includes zero means no change.

The run files stay outside this repository.

## Risks

- **A wrong seed for vague topic text.** Mitigation: `related` lists the other candidates, and the seed is named in the response.
- **Deep dives that never reached compiled notes,** such as those only in episodes. They surface only as cited Sources or as `related` hits. The evaluation measures how often that happens.
- **Rollback.** Revert the release. The sidecar is private metadata, and deleting it only resets every brief to `first_read`.
