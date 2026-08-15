## Context

The semantic-unit language already has two authoring forms and one normalized model. Compact observations carry a category, content, trailing tags, an optional parenthesized context, and an optional `^anchor`. Rich units are ATX headings whose label resolves to a governed kind, followed by leading `- key: value` metadata rows and a body. `semantic_blocks.BLOCK_TYPES` holds the twenty-six code-owned kinds; `semantic_language_registry.core_registry()` derives the core kind ring from that set plus the compact-only `observation`. Five metadata keys are reserved and understood today: `category`, `id`, `tags`, `context`, `relations`.

Three surfaces consume units. `structured_filters` compiles a bounded namespaced filter language over a closed `unit.*` field set and evaluates it against `unit_view()`, a small dict adapted from a live parsed `SemanticUnit`. `find` builds `SemanticUnitHit` objects whose `as_dict()` emits a fixed field list, gated again by the governed-egress projector `governance/egress.py::_UNIT_FIELDS`. `observe_memory` is the single-unit mutation route: it resolves an exact `unit_ref`, renders replacement Markdown, splices it into the parent body, and re-parses to assert the render round-trips.

That render is the problem. `observe_memory._render_unit` builds a rich unit from exactly `kind`, `category`, `content`, `tags`, `context`, `relations`, and `anchor`. An update reads none of the current unit's other metadata rows, so anything else the author wrote is deleted by the rewrite. `_assert_round_trip` then confirms the *rendered* shape came back, which means the deletion round-trips cleanly and raises nothing. No governed key other than the five reserved ones exists today, so the defect is currently unobservable — and would become a silent data-loss bug the moment this change lands.

Separately, the 2026-08-14 epistemic architecture audit fixed two boundaries this change must respect. Verdict is *state*, not supersession: a refuted claim is not a replaced claim, so it keeps `lifecycle: active` and full rank and stays retrievable with its refuting evidence. And there is no stored confidence, ever: the project rejects numeric credences on notes, so `verdict` is categorical and never a number.

## Goals / Non-Goals

**Goals:**

- Give the epistemic loop three primitives an agent can write, filter, and cite: a `prediction` kind, a `check_by` date, and a `verdict` judgment.
- Make a refuted result a first-class, fully-ranked, retrievable state that is distinguishable from an active unexamined one and from a superseded one.
- Close the six-field reconstruction defect before governed unit metadata can be lost to it, and close it generally rather than for these two keys only.
- Keep one vocabulary for unit-level `verdict` and page-level experiment `outcome`, so the same five words mean the same five things at both altitudes.
- Make `check_by` answer the question it exists for — "what is due" — which requires a typed date field, not a string.
- Keep Markdown canonical: every new field is parsed from an authored row, and no new index, sidecar, or store is introduced.

**Non-Goals:**

- No new relation kinds. `verdict` is a property of a unit, not an edge between units. The refuting evidence is attached with the existing `evidenced_by` / `refutes`-family vocabulary, unchanged.
- No new page types. `prediction` is a unit kind on existing compiled pages.
- No ranking effect. `verdict` never boosts, penalizes, or reorders a result, and never participates in fusion, recency, or prominence.
- No exemption from page-status inheritance. A unit on a superseded or archived page inherits that page's standing exactly as before, whatever its verdict.
- No stored numeric confidence, probability, credence, or score — not as a field, not as an alias, not as an accepted `verdict` value.
- No automatic verdict assignment. Nothing infers, decays, or expires a verdict; a human or an explicitly instructed agent writes it.
- No new audit or review queue. Surfacing overdue checks and unresolved concluded experiments belongs to the epistemic review changes downstream of this one.

## Decisions

### `prediction` is a code-owned core kind, not a registry extension

`semantic_blocks.BLOCK_TYPES` gains `prediction` and `_BLOCK_TYPE_ALIASES` gains `predictions`. The core kind ring is derived from `BLOCK_TYPES`, so the registry, the write contract, `observe_memory`'s `_select_kind`, the filter `unit.kind` resolver, and `schema_memory` all recognize it with no further wiring. That is the point: a governed kind is one edit in one table.

The alternative — shipping `prediction` in the scaffold's `semantic-language-registry.yaml` as a vault extension — was rejected. An extension kind is per-vault, can be deprecated or scoped away by the vault owner, and would make the epistemic loop optional in exactly the vaults that most need it. The loop primitives are product vocabulary, not local taste.

The cost is real and stated in the proposal's Impact: `_parse_definitions` raises `canonical_collision` when a registry extension shadows a built-in kind, and error findings invalidate the whole registry until fixed. A vault that already declared its own `prediction` kind must rename it. This is the established contract for every core-kind addition and this change does not weaken it, because weakening it would let a local extension silently shadow governed product vocabulary.

### `verdict` and `check_by` are metadata keys with parser-level grammar, surfaced as derived unit fields

They stay authored metadata rows — `- verdict: refuted`, `- check_by: 2026-11-01` — because that is what a semantic unit's metadata is for and it keeps Markdown readable. The parser validates them and, exactly as it already does for `tags` and `context`, projects the normalized value onto the `SemanticUnit` dataclass so downstream consumers do not each re-parse a raw string. `SemanticUnit.to_dict()` gains both keys, which is additive.

They are also added to `semantic_blocks._RESERVED_METADATA_KEYS`. That set decides which leading rows do *not* count as substantive body, so without it a `## Prediction` block containing only a `verdict:` row would look like it had content and would escape the existing `empty_rich_unit` error.

`verdict`'s vocabulary is the five audit-approved values — `abandoned`, `confirmed`, `inconclusive`, `qualified`, `refuted` — normalized by NFKC and casefold. Anything else, including any numeric string, is an `invalid_rich_verdict` error whose remediation names the closed set and states that confidence is not a stored field. Rejecting `0.7` with a message that explains *why* is the whole no-confidence guardrail at the authoring boundary.

`check_by` is a strict `YYYY-MM-DD` calendar date, validated by `date.fromisoformat` plus an exact round-trip equality check so `2026-1-1` and RFC 3339 instants are both rejected. A due-by date is a day, not an instant; admitting instants would force a timezone question the vault has no answer for.

Both keys are rich-only. Compact observations have no metadata rows at all, so `observe_memory` refuses `verdict` or `check_by` without an explicit governed non-observation kind, mirroring the existing `COMPACT_RELATIONS_REQUIRE_RICH_KIND` refusal rather than inventing a second shape.

### Reconstruction preserves every row `observe_memory` does not own

This is the change's load-bearing decision. `observe_memory` now reads the current unit's metadata, partitions it into the keys it owns — `category`, `id`, `tags`, `context`, `relations`, `verdict`, `check_by` — and the rest, and re-emits the rest verbatim after its canonical block. `_assert_round_trip` compares against the same merged expectation, so a dropped row now fails loudly instead of round-tripping cleanly.

Preserving *unknown* rows, not just the two new ones, is deliberate. Fixing this only for `verdict` and `check_by` would leave the identical bug armed for the next governed key, and would also mean `observe_memory` deletes a row an author wrote by hand that the parser simply does not interpret. Markdown is canonical; a mutation tool that quietly deletes authored bytes it does not understand is wrong regardless of which bytes they are.

The three new arguments are preserve-by-default on update: omitted means "leave whatever is there", and an explicit empty string is the only way to clear one. The existing `tags`, `context`, and `relations` arguments keep their current replacement semantics — omitting them still clears them. That asymmetry is uncomfortable but correct for this change: those three are a shipped, documented contract with tests that depend on replacement, and silently converting them to preserve-by-default would be a behavioural change to callers that this change was not scoped to make. The new arguments get the better semantics from the start, and the general unknown-row preservation means nothing is *lost* either way.

`id` joins them because an author who wants a stable, meaningful anchor (`^retry-budget-prediction`) currently cannot get one from `observe_memory` — the anchor is derived from a content hash. It is validated against the existing anchor grammar and rejected if it would collide with another unit's anchor on the same page.

One case has no good default: the current unit carries a governed row the parser *rejected*, and the caller supplied no replacement. There is no normalized value to preserve, and dropping the row is the exact silent deletion this design exists to prevent. So that update is refused (`INVALID_EXISTING_UNIT_METADATA`) with a remediation naming both honest exits — supply a valid value, or pass an empty string to clear the row deliberately. Refusing is the only answer that keeps "never silently drop" absolute rather than approximately true.

Changing a verdict changes the unit's fingerprint, because `_semantic_unit_signature` already folds all metadata except `id` into the signature. That is correct and free: recording a judgment is a semantic edit, so a concurrent writer holding a stale fingerprint is refused exactly as it would be for a content edit.

### `unit.check_by` is a typed date field; `unit.verdict` is a closed string

`page.updated` is already the filter language's one typed date field, and it is special-cased in five places: `_KNOWN_SCALAR_FIELDS`, the `$contains` rejection, `_parse_scalar`'s temporal forcing, `_validate_closed_field_operand`, and `page_view`'s normalization of the raw frontmatter value into a real `date`. Rather than add a sixth special case for one more field, those sites now consult a `_KNOWN_DATE_FIELDS` set holding both. `unit_view` normalizes `check_by` to a `date` object for the same reason `page_view` does: `_runtime_scalar` types a leftover string as `"string"`, which would silently never compare equal to a `date` operand and would drop every unit from every date filter with no warning.

`unit.verdict` is added to the closed string fields and canonicalized by strip-and-casefold, like `page.status`. It gets no index-candidate seed: `plan_index_candidates` seeds only `unit.category` and `unit.kind`, and a verdict predicate post-filters exactly as `unit.tags` and `unit.context` already do. Adding a third seed axis would be a retrieval-architecture change this proposal has no evidence it needs.

`unit_view` omits both keys when absent rather than emitting `None`. That differs from the existing `context` handling, where an absent context is present-as-null and therefore satisfies `$exists: true` for every unit. For these two keys absence is the meaningful state — "no verdict yet", "no check date" — so `$exists` has to be able to say so. The existing `context` quirk is left alone; changing it is not this change's business.

### The experiment page type gains `concluded` and `outcome`, and no tool parameter

`STATUS_EXPERIMENT` gains `concluded`, so an experiment can say it is finished without being archived — `archived` means "stepped out of active rotation", which is a different claim. The existing `concluded:` date field is unaffected and complements the status: one says *when*, the other says *where in the lifecycle*.

`outcome:` uses the same five values as `verdict`. One vocabulary at both altitudes means a reader never has to translate between a unit's judgment and its parent experiment's judgment.

`outcome` is deliberately **not** added as a `remember` or `replace_memory` parameter. This change's accepted tool-surface movement is scoped to `observe_memory`'s governed unit-metadata arguments; widening it to the creation writers would move more of the pinned schema than the approved design calls for. `outcome` is authorable through the existing `edit_memory` `patch_frontmatter` route, and that route is where the enum is enforced: an invalid value, or `outcome` on a page that is not an experiment, is refused. An accepted spelling is normalized on the way in, so a vault never ends up holding `Refuted` and `refuted` as if they were different states. Since the field is new, there is no prior behaviour to regress.

### The semantic authoring contract is not modified

`semantic-write-contract`'s delta here is behavioural — the new kind is recognized like any other governed kind, and neither it nor its metadata adds any quota — and both properties already hold through existing machinery: the minimum-unit predicate accepts any valid unit of any governed kind, and the contract's only count is `minimum_count: 1`.

Editing `semantic_authoring`'s normative payload would bump `AUTHORING_CONTRACT_VERSION` and its content digest, and that digest is embedded verbatim in roughly twenty shipped `SKILL.md` files, `docs/capabilities.md`, `docs/semantic-language.md`, and two pinned test constants. Regenerating all of that to assert a property the contract already has would be a large, orthogonal diff with real drift risk and no behavioural gain. The capability is therefore specified and proved by tests over the existing contract rather than by new contract prose. If a later change needs the contract to *teach* the loop primitives, it can pay the version bump deliberately.

## Risks / Trade-offs

- **A vault with a custom `prediction` registry kind breaks until renamed.** `canonical_collision` is an error finding and invalidates registry resolution. Accepted: this is the standing contract for core-kind additions, and the alternative (letting a local extension shadow product vocabulary) is worse. The finding is explicit, path-addressed, and names the collision.
- **Preserving unknown metadata rows preserves typos too.** A misspelled `- verdcit: refuted` now survives an update instead of being cleaned away. Accepted, and preferred: silently deleting authored bytes is a worse failure than carrying a typo the author can see and fix.
- **Preservation is only as complete as the parser's metadata model, which collapses duplicate keys.** `semantic_blocks._split_metadata` stores rows into a dict, so a unit authored with `- reviewer: first` *and* `- reviewer: second` reaches every consumer as `reviewer: second`; the first row is already invisible before any mutation. Reconstruction faithfully re-emits what the parser produced, so it drops nothing itself, but it cannot resurrect a row the parse already discarded. Fixing that means turning unit metadata into a multimap, which is a parser-model change well outside this proposal. Recorded so the preservation guarantee is not read as stronger than it is.
- **The pinned tool surface and the connector fingerprint both move.** The ChatGPT Personal Plugin pending digest changes, and it stays release-blocking until that external consumer is refreshed and verified. This is called out in the proposal rather than absorbed silently, because a moved fingerprint that nobody notices is how a connector rollout goes stale.
- **`verdict` participates in the unit fingerprint, so recording a judgment invalidates a concurrent writer's guard.** Intended — a verdict is a semantic edit — but it does mean a caller that read a unit, thought about it, and then wrote a verdict may need to re-read. The existing stale-reference error already explains that.
- **Preserve-by-default for the new arguments but replacement for `tags` / `context` / `relations` is an inconsistent contract.** Documented in the tool help and the spec rather than hidden. Unifying it belongs to a change that is scoped to alter shipped argument semantics.
- **`check_by` is day-granular only.** A vault that wants "check after the sprint demo at 15:00" cannot express it. Accepted: a due-by day is what a knowledge base can actually answer, and admitting instants would require a timezone policy the vault does not have.

### The frozen v1 hosted descriptor tracks the surface, not the tool list

`observe_memory` is a member of the `hosted-alpha-agent-v1` hosted profile, and `hosted_plugins.compatibility_manifest` derives the committed descriptor from the *live* command registry filtered by that profile. Moving `observe_memory`'s schema therefore moves `compatibility_sha256` — which `hosted-plugin-identity` explicitly requires ("a schema digest change → `compatibility_sha256` changes") and `check_compatibility_descriptor` enforces. But `tests/fixtures/hosted/v1-release-identities.json` pinned those derived bytes as immutable, and `hosted-client-plugins` calls v1 "the existing immutable profile". Two committed requirements, one artifact, opposite demands — and this change is the first to collide them.

The collision was escalated rather than settled locally, and the decision is: **let v1's derived identity track the surface.** The descriptor describes what the profile actually serves. v1 clients genuinely do see the new `observe_memory` schema, so a descriptor that did not move would be advertising something untrue. Pinning a stale descriptor while the served schema drifts is strictly worse than letting the descriptor follow it, because a consumer trusting the pinned identity would be reasoning about a contract nobody serves.

The alternative — freezing schemas per profile rather than only the command list, so that "frozen" delivers what the word implies — is a new capability in the hosted identity model, not a fix belonging to this change. It is filed separately.

What actually moved is narrow, and was verified rather than assumed. Only `agent_contract.commands[5]` (`observe_memory`) changed, gaining exactly `verdict`, `check_by`, and `id` and losing nothing. The profile's command *membership*, its `command_surface_sha256`, `skills_sha256`, `definition_sha256`, profile id, and the v2 reader floor are all unchanged, so the sense in which v1 is "the immutable profile" — which commands it serves — is preserved. Both platform locks moved only because they embed `compatibility_sha256` and `schema_contract_sha256`. Every package archive is byte-identical, and the definition, both promotion records, and the bundled skill are untouched. v1 promotion state is `pending`, so no live registration is invalidated. The same reasoning and the same verification apply to the `hosted-alpha-agent-v2` candidate, which this change also re-renders because it was equally stale for the same reason.
