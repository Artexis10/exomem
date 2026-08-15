## Context

Four product tools mutate compiled notes: `remember`, `edit_memory`, `observe_memory`, and `replace_memory`. They fan into exactly two commit functions in one module — `semantic_writes.commit_creation` and `semantic_writes.commit_existing` — whose results already reach the caller as `leaf_result["creation"]` and `leaf_result["semantic"]`.

Three facts from the current source shape this design.

First, `write_feedback` is not a viable carrier. It is built only in `note._build_write_feedback`, so it exists for `remember` alone, and `mutation_terminal.project_terminal` is a strict allowlist over `_ENVELOPE_KEYS` that never merges `leaf_result` into a compact response. Every writer defaults to `response_detail="compact"`, so `write_feedback` reaches no caller unless one explicitly asks for `full` or `legacy`. Attaching structural advice there would cover one of four writers and would be invisible in the default response of that one.

Second, the evidence is already in memory. `semantic_contract.SemanticCorpusContext` is built and process-cached on every write and retained on the preflight as `before_corpus` and `after_corpus`. Its `SemanticPageState` carries `page_type`, `projects`, `status`, `title`, `frontmatter`, `body_wikilinks`, and the fully parsed `SemanticUnitDocument`, whose source-ordered units each expose `category`, `kind`, `tags`, `content`, and `anchor`. No read, query, or index is required to inspect the page that was just written.

Third, there is no per-page mutation history recording which units a given write added. `log.md` records an operation, a date, and a rationale per page, but costs a whole-file read and retains only the newest entries vault-wide; the `mutations` table is idempotency state. A detector that wants "the same off-scope topic recurring" therefore cannot count write events cheaply.

## Goals / Non-Goals

**Goals:**

- Detect that a just-written compiled page has accumulated recurring durable material outside its own declared scope.
- Reach all four compiled writers from one place, without editing the writer leaves or the writer-lease safety core.
- Make the signal visible in the default response, since an advisory nobody receives is not a feature.
- Keep the payload explainable: named reason codes and counts a human can check against the page.
- Fail silent and stay advisory under every error, refusal, or missing optional state.

**Non-Goals:**

- Proposing, naming, or creating a destination page, hub, or project.
- Moving, splitting, retitling, or rescoping any file.
- Emitting a numeric confidence, score, or probability.
- Persisting suggestion state, acceptance, dismissal, or cooldown.
- Any whole-corpus scan, background job, embedding, or model call.

## Decisions

### Carry the suggestion on the two commit results, not on `write_feedback`

`CreationCommit` and `ExistingCommit` gain one optional field, emitted from `as_dict()` only when a suggestion exists. `remember` and `replace_memory` surface it at `creation.structure_suggestion`; `edit_memory` and `observe_memory` surface it at `semantic.structure_suggestion`. Both already flow to the terminal, so no writer leaf changes.

`project_terminal` then lifts the suggestion from either location into the compact envelope, following the existing precedent that projected bounded `warnings` texts and the `graph_sync` outcome fields out of the leaf. `_ENVELOPE_KEYS` is not extended: those keys are the closed set a client may branch on for the outcome of the mutation, and a structural advisory is not part of that machine. The live Records acceptance allowlist gains the key as an optional compact field.

The alternative — hooking `writer_lease.invoke_leaf`, the single highest common layer — was rejected. It sees `leaf_result` and the vault root but not the parsed page, so it would have to re-read and re-parse the file it just wrote; and it is the module holding receipts, fencing, commit evidence, and idempotency, where a post-commit exception converts a committed write into an uncertain outcome.

### Compute after the guarded section, from state already held

The detector runs inside the commit function but after the locked write completes, over the page state the preflight already produced. It performs no file read, no database access, and no network or model call, so the mutation critical section is unchanged and the pure-substrate constraint is satisfied by construction: the server measures the page it just wrote, and the agent reasons about it.

The whole call is wrapped in a bare exception guard that logs and returns nothing, matching the established idiom for optional post-write work. A detector fault cannot change `status`, `mutated`, `warnings_count`, or any existing key.

### Judge units against declared identity, never against length

The page's declared identity is the union of its frontmatter tags, the significant tokens of its title, and its project keys. A durable unit is off-scope when its own tag vocabulary is dominated by terms outside that identity — strictly more out-of-scope terms than in-scope ones, and at least two out-of-scope terms, so a thinly tagged unit cannot qualify on one stray word.

Unit tags alone carry the topic signal in v1. Categories are deliberately excluded: the vocabulary is open, and many categories name an epistemic role such as decision or constraint rather than a subject, so admitting them would mix two different axes for no gain on the motivating case. A page whose units carry no tags therefore produces no signal, which is the correct default for an advisory.

Off-scope units are then grouped by shared vocabulary, and a term only seeds a group when it appears in at least two off-scope units. That recurrence requirement is what separates a genuine emerging topic from an incidental tangent, and it is why one or two off-topic observations cannot trigger.

Four reason codes are emitted, all deterministic and independently checkable:

- `off_scope_cluster_recurs` — the group's seeding vocabulary appears across multiple off-scope units.
- `declared_scope_mismatch` — that vocabulary is absent from the page's declared identity.
- `cluster_reaches_child_note_mass` — the group has enough units and enough distinct recurring terms to justify its own note.
- `page_retains_original_scope` — enough units still match the declared identity, so the page is genuinely mixed rather than simply misnamed.

At least two codes are required to emit anything; all four are required for `strong`. A single signal never produces a suggestion.

### Recurrence is measured over units, not over write events

This is the design's known approximation and it is deliberate. Units are what writes deposit, so counting recurrence across durable units tracks the same underlying accumulation, and it costs nothing. It does mean a single write depositing many units is treated like several writes depositing one each. The reason codes and the specification therefore speak of units, never of writes, so the contract does not claim history it does not read.

### Exclude by declared identity rather than by heuristic

Only compiled note types are eligible; sources, evidence, media, records, planning, and structured collections never enter the path. Navigational pages are excluded. A page tagged as a deliberate hub or snapshot is excluded, reusing the existing convention that already exempts those tags from staleness and activation pressure.

A correctly built hub is additionally quiet on principle rather than by exemption: it declares its breadth in its own tags and title, so its units are in scope and no divergence exists. The same property produces the post-restructuring behaviour — once material is routed into a destination whose declared identity matches it, neither the new page nor the cleaned original diverges, and the advice stops without any dismissal state.

### Report only what the caller just wrote

Every value in the payload — the reason codes, the off-scope unit count, and the bounded list of recurring terms — is derived from the page named in the same response. No other page's path, title, project, or count is read or reported. The governance requirement is met structurally: there is nothing in the payload that a caller who performed this write could not already see, so no disclosure decision is needed on the write path.

## Risks / Trade-offs

- [Unit-recurrence stands in for write-recurrence] -> Name units in the contract and reason codes so nothing overclaims; revisit only if a cheap per-page write history appears.
- [Untagged units carry no vocabulary] -> Such pages simply produce no signal. The failure mode is silence, which is the correct default for an advisory.
- [Synonym drift could read as topic drift] -> A page whose units consistently tag the declared subject with different words than the frontmatter does could look divergent. Partly mitigated: if most units drift that way, `page_retains_original_scope` fails and nothing is emitted; if only a few do, the group cannot reach mass. It remains the most plausible false positive, and the honest first calibration target.
- [A page whose title and tags are stale could look divergent] -> Require `page_retains_original_scope`, so a page that has wholly moved on reads as misnamed and stays quiet rather than being told to split.
- [Per-write cost is added to the commit path] -> The detector is a bounded pass over units already parsed and held in memory, with no I/O; the existing write-latency ceilings and the superlinear-scaling bound remain the gate.
- [A new compact key could be read as part of the outcome machine] -> Leave `_ENVELOPE_KEYS` unchanged and document the field as advisory, exactly as `warnings` and the graph outcome fields are.
- [Advice could become nagging across a conversation] -> The runtime reports the condition; the agent contract carries in-conversation suppression, and durable dismissal state is deferred rather than invented here.
