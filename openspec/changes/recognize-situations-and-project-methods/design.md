# Design

## Context

See `proposal.md` for the outcome. The current implementation has several useful but incomplete paths:

| Responsibility | Existing owner | Observed gap |
| --- | --- | --- |
| Orientation and profiles | `context_catalogue`, `context_views` | The top map omits ordinary notes; profile methods depend on an anchor neighborhood. |
| Turn leads | `working_set`, `context_views.turn_leads` | The slice explicitly excludes recent context and relies on anchor-oriented discovery. |
| Recent work | `working_set._recent_context`, heat projection | Contact recency does not establish relevance, commitment or current state. |
| Conversation interpretation | `working_set_conversation`, agent focus | Focus supplies lexical contact; history mostly qualifies already discovered candidates. |
| Methods | Semantic units, method roles, ordinary notes | Existing contracts promise selection and adaptation, but portable revision delivery is missing. |
| Workflow policy | `workflow_contracts` | Structured ownership and capture policy intentionally differs from method knowledge. |
| Local sensing | `sensor_worker`, `sensing`, `sensed_model` | Existing pair judgments are durable epistemic readings; they cannot be reused as turn-specific ranking. |

These findings identify mechanisms, not a completed causal diagnosis of every field report.
The original incident inputs and source chronology remain private development evidence.
Later repair notes must not enter a replay of an earlier failure.

## Goals / Non-Goals

**Goals:** A fresh client can discover a relevant ongoing situation and applicable method without the user naming their stored homes.
An authorized reusable method edit affects the next relevant interaction through Exomem.
The system scales through bounded discovery, reads and refreshes over one knowledge substrate.

**Non-Goals:** This change does not supply a fixed library of domain workflows, execute workflow code, mirror companion task queues or create commitments from inferred intent.
It does not promise delivery to a client that never calls Exomem.
It does not migrate the epistemic sensing ledger or enable an API provider by default.

## Decisions

### D1. Discover across carriers before judging task fit

Add one bounded candidate assembly stage to the existing maps activation path.
Reuse the existing retrieval lanes, graph traversal, collection claims and catalogue; do not build another search engine.
Search the current turn and available agent focus independently of anchor resolution, retaining their distinct origins.
Ordinary compiled notes, relevant episode sources, supported Planning references and method knowledge remain eligible even when no anchor resolves.
Use registry definitions and declared roles for eligibility; do not encode domains, workflow names or user phrases in code.

Combine independent discovery results by canonical identity and source version before the final budget allocation.
An exact anchor match is evidence of contact, not proof that its topic exhausts the user's intent.
Relevant older dependencies remain eligible beside recent candidates.
Contact recency supplies a bounded discovery input; it never labels a candidate as ongoing by itself.

Collection descriptions and schemas provide routes into exact Records and Planning queries.
Read rows only after identifying a relevant collection and a bounded query.
The compiler must not index or enumerate arbitrary high-volume rows to improve general recall.

The cheaper baseline remains the existing map-assisted agent pulls.
The new stage must improve the missing-candidate cases without requiring a model or forcing every context layer into every turn.

Restricted callers rank only a complete corpus from canonical released FULL sources.
Reuse source terms through the existing admitted-corpus statistics and postings code; keep this representation distinct from raw lexical text.
A current effective caller basis and complete source-release proof must justify each reused row.
A raw source hash, caller binding, conversation identifier or earlier grant alone cannot supply that proof.
The proof binds hashes produced by the exact reads that the release owners classified.
A namespace or reference proof binds the owner snapshot and resolution that produced the representation.
It must not certify that representation by reading a different current snapshot afterward.
A pre-read guard cannot certify a later decision if the source changes and returns to its earlier bytes.
Conflicting observed hashes decline reuse without changing canonical read order.
Revalidate the incoming principal and authorization session on every request; never retain a bearer or caller object.

The egress owner records a closed set of supported release dependencies where it resolves them.
These include source and target admission, denied targets, unresolved names, path variants, registry state, artifact names and ownership.
Literal artifact scans require a complete names-namespace fence, including names that did not match previously.
Proof lookup remains read-only; it does not rebuild a census to make cache reuse possible.
A row that consults managed origin evidence stays uncacheable until its owner supplies a versioned proof.
Unsupported or unknown proofs use canonical preparation; a cache miss does not create a new refusal.

Keep only public versions, ordered term counts and private proof facts after projection; discard source bodies and full token lists.
Stream tokens through the maintained tokenizer owner and preserve its one-shot output contract.
Admit normalization workspace, counter growth and packing overlap before allocation.
Whole-stretch normalization preserves combining sequences; bounded iteration must not split that semantic operation.
Reproject affected rows from complete freshness history and reverse dependencies.
An ordinary source edit must preserve reusable rows whose dependencies did not change.
Every successful reprojection atomically replaces its raw guard, complete proof, omission facts and reverse dependencies.
If its public version stays identical, statistics may remain unchanged.
Selected candidates and held methods still hydrate through current canonical source projection before delivery.

The existing semantic preparation reservation bounds guarded reads, parsing and retained corpus state.
Account for terms, proofs, reverse dependencies, transient buffers and overlapping publication generations within the existing allowance.
Count shared immutable rows once within an accounting epoch, using compact identity metadata that also consumes the allowance.
End the epoch before removing a registered object, and rebuild accounting after the invalidation batch.
Register the final installed row; a failed measurement must not leave identities for discarded temporary objects.
Measure mutable containers, request memos and capture state separately, including their resize overlap and visitor workspace.
Keep one row reservation through canonical projection, proof completion, packing and transfer to resident state.
After canonical projection returns, replace completed parser claims with measured live output and capture state before proof completion.
Measure the whole source projection, capture and memo; include the completed proof before packing.
Nested live claims add together; an inner preparation cannot release its parent's charge.
Transfer only credit that no longer protects live transient data, and charge each workspace once.
The existing read policy snapshot covers candidate assembly only; optional inference and terminal guards run after that scope closes.
An expired request may retain individually complete rows and private aggregation progress, but must publish no prefix statistics or rankings.
Under an unchanged effective basis and complete dependency history, resumption must advance validation and aggregation without restarting the retained prefix.
Never continue caller-authorized reads after the request ends or synthesize a caller session for background work.
Cold preparation may be unavailable; the unchanged latency gate decides whether the implementation qualifies.
Exhaustion preserves maps and ordinary pulls without asserting source absence.
Existing unit release decisions remove withheld unit spans before the existing BM25 scorer sees their source text.
Visible units and uncovered prose remain discoverable on mixed pages; no raw top-k or title-only prefilter selects this corpus.
Unrestricted callers retain the maintained retrieval fast path.
A preparation deadline or capacity failure leaves the new task lane unavailable without reporting source absence; maps and ordinary pulls remain usable.

Excerpt-only and lower-release sources consistently stay outside this new corpus and method continuation.
Extra unit omissions have no exact mapping to the canonical fixed L5 excerpt, so this change does not refill or approximate that window.
Discovery reports partial coverage and preserves existing canonical read, find and map pulls.
This limitation does not depend on whether the source contains withheld units.
Restricted task graph expansion reports an omitted lane: existing graph caps and page edges precede origin-unit release.
Unrestricted graph expansion stays unchanged. Restricted collection claims require the existing FULL metadata admission; text recall remains independent.

### D2. Preserve candidate, current-state and intention distinctions

Each selected candidate carries its canonical ref, source version, carrier, origin and an honest reason for discovery.
Topic similarity and recent contact remain leads rather than claims of relevance or continuing intent.
The active agent opens sufficient evidence before using a lead as an answer premise.
The compiler resolves declared state through the existing authority; unknown state stays unknown.

For an apparent continuation, inspect linked plans, newer corrections, supersession and closure evidence through existing reads.
An undecided purchase remains undecided. A cancelled plan remains cancelled despite a recent edit.
An unfinished episode is evidence about the conversation, not a second task store or an implicit commitment.
Failed or incomplete discovery cannot justify an assertion that no canonical home exists.

### D3. Methods remain user-authored governed knowledge

Use existing semantic units, procedure blocks and governed pages as the canonical method representation.
Public reads of methods in optical inspection and seed comparison already return their authored units.
Activation lacks portable method identity, whole-source revision and applicability completeness fields.
An optional role-registry `projection: method` attribute selects the implemented method packet shape.
The registry supplies its categories; code never selects method knowledge by a category or lens name.
Use the method role and the vault's semantic registry to discover them outside the current anchor neighborhood.
Do not add a method directory, separate skill database or a new workflow-contract family.

A method projection carries identity, page/source version, attribution, applicable scope and the refs needed to read its operative content.
Authored applicability and exclusions travel with that content; an incomplete projection tells the agent to read the page before applying it.
The first implementation binds the whole source page version, including applicability outside the selected technique unit.
It does not infer applicability from a title or silently treat missing exclusions as an empty list.

The active agent selects and adapts methods in context. A current user instruction can override a reusable method for this task.
Persistent revision uses the existing permitted writer with its current source guard.
Local adaptation does not revise the reusable method unless the user or standing capture policy authorizes that change.
Method text can guide behavior within existing authority; it cannot grant permissions or turn captured source instructions into operating policy.

### D4. Refresh behavior by source revision, not map generation

Track the bounded set of delivered method refs and source versions through the existing activation continuity token.
On every activation carrying that set, revalidate its admitted sources before current-task discovery or applicability filtering.
This ordering detects retirement and changed exclusions even when a method no longer matches.
Return admitted invalidation before selecting current content; suppress unrelated method content after revalidation.
Return changed content or a clear withdrawn/superseded state instead of repeating a stale method.
A change to applicability alone invalidates the projection.
Withheld material follows the existing absent-equivalence contract and never produces a revealing removal notice.

An unchanged map is independent of method freshness; managed stale-map serving cannot certify current behavior.
Generic MCP, REST and CLI clients receive the same projection. Hooks render that result rather than maintaining separate method files.
The shipped skill defines when to refresh and read; user methods remain live knowledge.
No repeat bootstrap is required merely because a method changed.

Use an optional `method_versions` list of `{ref, source_ref, source_version}` in the existing version-one token.
The combined anchor-ref and method-entry count stays within the existing 32-entry ceiling; the encoded token stays within 8,192 characters.
These entries are untrusted read hints, never anchor evidence, identity proof or permission.
Recheck every reference through the caller's current view; preserve the existing thread binding and lifetime rules.
Old tokens carry no methods, and old readers ignore the optional field.
Malformed method entries are ignored without losing the independently valid map token.

The packet's `task_context.methods.reset` tells a client to discard its held method projections before consuming this response.
Use it when a carried source is missing, withheld, changed, retired or cannot be checked within the request allowance.
Missing and withheld sources produce the same generic reset, without revealing a reference or reason.
Current admitted replacements follow ordinary selection; an unrelated replacement need not be sent.
If the token or packet cannot carry every retained method dependency, reset the set and carry only complete new projections that fit.
Thus eviction never silently certifies a stale method. Clients that omit continuity must freshly read a method before reuse.

### D5. Keep one shared budget and selective intervention loop

Maps orient, profiles provide depth, stance supplies applicable standing constraints, and candidate context supplies the current task's leads.
Use the existing prose budget, serialized-size report, continuation and `next_action` mechanism.
The map keeps the existing fixed 25% reserve so its allocation stays independent of the turn.
Task context and the turn slice share this reserve.
Preserve the complete guarded slice within this reserve; task context uses the actual remaining allowance.
Omit generic task pointers only when the slice retains their routes.
This preserves existing contact and due-item routes that duplicate task pointers had crowded out in development.
Compile stance before dividing the remaining material allowance. Reserve next-action prose before compiling material.
When task context runs, stance can use only the space above mandatory map framing divided by one minus the fixed share.
The earlier stance cap left only map framing; taking a share afterward crowded mandatory framing and invalidation out of their allowance.
This minimum framing constraint depends on the cap and serving surface, never the turn, held methods or optional model.
Keep existing stance selection and honest omission counts within that allowance; standalone stance behavior is unchanged.
The reserve also applies when the slice is disabled.
Unused task capacity remains available to the slice, without changing the turn-independent map.
Do not increase the packet cap to hide poor selection.

Initial activation runs once. The active agent refreshes when evidence, the subject, the task or uncertainty changes materially.
It preserves the human turn and labels its own focus and refresh origin.
Ordinary read replies alone do not trigger refresh loops.
Unavailable discovery or interpretation leaves existing map and pull routes usable, with honest partial status.

Restricted FULL source versions digest the complete canonical released body and metadata, including visible exclusions outside selected units.
They do not digest a summary, unit-only projection, token list or private raw hash. Guard records retain the exact raw hash privately.
Unrestricted FULL callers retain the existing whole-source version. No exact hash or invented title, carrier or attribution crosses below FULL.

Current method identities reuse the existing unit release and currency chain once per admitted source/version.
Apply unit admission before same-page supersession and typed expiry; review-due units remain current read routes.
The selected semantic parent must match the same whole-source version that the packet carries.
Revalidation shares this selection before discovery filters, so matching source bytes cannot certify a retired unit.

### D6. Fix the small wire contract before optional interpretation

The shared prose counter charges map, stance, task context, slice and next-action prose once each.
Task pinning binds the full public cap, including the space reserved for next action.
The hook renders a fixed reset line before optional routes and retains the prior token if that reset cannot fit.
Private served-context records retain task states, source identities, versions and read routes without source or prompt text.

Add `task_context` beside `map` and `turn_slice`; do not redefine guarded anchor evidence as general relevance.
Its closed protocol state is `ready`, `empty`, `partial`, `unavailable` or `skipped`.
It contains `candidates`, `methods` and the existing section-level `missing` reasons, without hidden names or counts.
Candidates carry `ref`, `source_ref`, `source_version`, `carrier`, `origins`, `reason` and `read_ref`.
Carrier values come from existing registries. Origins describe the retrieval input or graph edge, not a judgment about intent.
The reason uses existing categorical evidence or an honest read-required description, never an invented claim.
Reuse current-state projections and linked evidence routes where available; omit unavailable state rather than inventing a new status taxonomy.

`methods` contains `reset` and `items`.
Each item carries `ref`, `source_ref`, `source_version`, `attribution`, `applicability` and `read_ref`.
`applicability` reports `complete` or `read_required`; complete means the projection includes all operative scope and exclusions.
An optional `content` contains existing governed unit or procedure content, not a new method document.
A partial page projection requires the source read even when its selected unit fits.
Identity and version describe the actual served source, not an earlier catalogue snapshot.
Candidates and methods share existing caller admission, source guards, prose charging and final serialization accounting.
The original turn-slice setting controls only that slice; it does not control this separate context contract.

### D7. Add one optional context-fit boundary

The first optional model job judges whether an already discovered candidate applies to the current task.
It receives bounded, released candidates and bounded ephemeral conversation context.
Its output is an `items` list of `{ref, source_version, fit}`; `fit` is the closed protocol enum `relevant`, `uncertain` or `unrelated`.
A missing candidate result means abstention (`uncertain`). An empty valid result leaves the baseline order intact.
An unknown identity, mismatched version, duplicate identity or invalid label rejects the complete result and preserves baseline order.
Validated labels only reorder the supplied candidate pool: relevant first, uncertain next, unrelated last, preserving baseline order within each group.
They do not admit, delete or rewrite candidates, affect anchor resolution, or reorder method invalidation.
The shared budget may still omit lower-ranked content; normal read routes and honest overflow remain available.
The packet reports interpretation as `disabled`, `ready` or `unavailable`; labels remain advisory to the active agent.
The interpreter does not generate queries, plans, summaries or new facts.
It cannot invent candidates, resolve identity by fiat, override explicit user context or convert similarity into commitment.

This is a new request-scoped interpretation contract, separate from durable epistemic sensing.
No turn text, conversation hash, model prompt or turn-specific reading enters that sensing ledger or a persistent cache.
Content-only preparation can reuse existing derived indexes with their source invalidation; no new background semantic compiler is required for this stage.
The active agent remains the interpreter of the returned evidence.

Provide a narrow typed adapter over a maintained structured-output runtime, using the existing HTTP client and schema validation.
Local execution is the preferred placement; an explicitly configured API backend implements the same contract.
Ollama and llama.cpp already provide structured-output serving, so Exomem does not need to own model serving or grammar generation.
Their interfaces still require integration tests and local output validation; a successful HTTP response is not a valid judgment.
Model/runtime identity, template version, placement, deadlines, token/input bounds and resource allowance are deployment data.
No model name, hardware assumption, domain vocabulary or API secret is compiled into the selector.

The adapter is default-off. A disabled, missing, invalid, timed-out or over-budget provider falls back to the same candidates and agent path.
It never extends the request deadline, retries indefinitely or silently falls back from local processing to a remote provider.
Caller admission precedes candidate selection and every provider input; API disclosure authorization is an additional requirement.
Restricted views cannot consume owner-built rankings, counts or cached interpretations.

The existing no-model conversation clauses receive only this explicit context-fit exception.
Other conversation retention, anchor evidence, canonical authorship and mutation contracts remain in force.

Provider input contains only admitted canonical identity, source version and released source prose.
Existing reference redaction and provenance projection run under both caller and provider views before inference.
Candidate display metadata stays in the client response and does not enter model input.
An API call uses the caller/provider intersection projection and its admitted reference/version.
A private request-local binding maps the validated order back to unchanged caller candidates.
A judgment grants no admission, source currentness, authority or applicability for omitted method clauses.
The existing Pydantic parser validates consumed HTTP envelopes; malformed or deeply nested success replies retain baseline order.
Preparation checks the existing request budget between source stages, and dispatch recomputes the remaining HTTP allowance after preparation.

### D8. Prove the user workflow, then retain only useful machinery

Reuse the existing actor transport, source custody and blind judging.
First preserve the pre-incident source set and trace original input, available candidates, delivered packet, subsequent reads and answer.
Missing original traces stay missing; current successful search is diagnostic, not proof of original availability.

Use small development rounds with one mechanism changed per round:

1. Compare existing pulls with broader candidate discovery on the same source-bound cases; expect the missing evidence to become reachable.
2. Exercise two fresh clients around a governed method edit; expect changed behavior and unchanged unrelated tasks.
3. Compare the same candidate set with and without optional context-fit inference; expect evidence of benefit before retaining that stage.

Include a cancelled intention, an undecided idea, competing plans, a similarly named person/product, unrelated recent edits and a method with an exclusion.
Include a correction outside the selected unit, restricted-view twins, malformed model output, refusal, unexpected input and provider failure.
Measure source reach, correct use, inappropriate activation, correction burden, latency and local resource cost separately.
Report paired case differences and uncertainty; an interval containing zero establishes no gain.
Do not reuse old sealed evaluation inputs, turn repaired outputs into answer keys or relabel development cases as held-out evidence.

## Risks / Trade-offs

- Broader discovery can add noise. Bound each source and preserve candidate status; compare false activations as well as recall.
- Whole-page method versions can cause extra refreshes. Prefer correct invalidation first; optimize only if measured churn warrants finer dependencies.
- Local inference can add latency or memory pressure. Keep it optional, bounded and outside the write path; validate cost on the actual placement.
- Clients can ignore valid context. Prove ordinary first answers and cross-client behavior, not only packet contents or scripted calls.
- Method text can contain hostile instructions. Admit governed method knowledge, preserve provenance and retain the client's existing authority boundary.

## Migration Plan

The parent maps change has shipped runtime contracts that are not yet in canonical `context-activation`.
Synchronize that existing delta before applying this change's map modification; keep its open replay, evaluation and recovery tasks unchanged.
After integration, refresh overlapping parent map and activation blocks from the resulting canonical requirements so later archives cannot revert them.
This synchronization records existing contracts and adds no replay or approval gate.

1. Complete the existing stance increment independently; expect its source and delivery contracts to remain available.
2. Integrate discovery and method refresh in one reviewed batch; expect current public operations to carry the new bounded context.
3. Qualify the optional adapter separately; expect model-free operation to remain complete when the adapter is off.
4. Run current CI and ordinary real-model journeys; expect source-bound evidence before merge and release.
5. Publish through the ordinary release owner; expect installed-byte verification only where live installation is already authorized.
6. Restore the previous activation setting or release on regression; expect canonical knowledge and methods to remain intact.

The optional adapter's deployment configuration is its rollback; disabling it restores the agent-only interpretation path.
Live service installation, historical replay and retained evaluation cleanup keep their separately recorded authority and dependencies.
