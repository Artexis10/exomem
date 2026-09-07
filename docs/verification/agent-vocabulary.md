<!-- authority:non-specification -->

# Agent-led vocabulary verification

This record accompanies `activate-agent-led-vocabulary-evolution`. The source
booklet, ordinary prompts, and evidence-based expectations are in
[`vocabulary_ordinary_cohort.json`](../../tests/fixtures/vocabulary_ordinary_cohort.json).
The booklet is synthetic. It contains independent reports of a supplier, two
ongoing programmes and their administrators, an existing organization alias,
an incidental personal name, and an explicitly unsupported programme-to-programme
relationship. Expected outcomes follow those facts; there is no target number of
types, entities, or edges.

## Ordinary-agent observations

Each trial used a disposable cell through the public REST API. Agents received
an ordinary domain task, fetched the served bootstrap and source reports, and
could consult OpenAPI. They did not read implementation or test fixtures.
Later trials used frozen package copies so ongoing source edits could not mix
module versions inside a running server. Embeddings were disabled.

| Trial | Actual outcome | What it establishes |
| --- | --- | --- |
| Initial organization task | Created a school organization and a sourced note; treated the entity-type resolver's empty selection as a blocker | Resolver availability alone did not lead to new-type adoption |
| Revised resolver guidance | Saved a sourced insight and reused the existing organization identity | Useful capture succeeded; the vocabulary recovery route was ignored |
| Confirmation-enabled organization task | Proposed Concept entries, then proposed and saved a Programme type after domain feedback distinguished real activities from abstract concepts; created the school and two programmes, enriched the existing cooperative, and saved a summary note | Assisted custom-type adoption, canonical validation, authored use, reuse, and enrichment succeeded; formal vocabulary review/decision correlation was not used |
| Explicit compact workflow cadence | Saved a sourced insight; supplier and administrator queries retrieved it | The agent admitted substituting resolver results for the required review/decision cadence and ignoring the returned recovery route; this is an adoption failure, not a successful workflow trial |
| Profile and navigation task with operating instructions first | Independently proposed Programme and grounded relations, then created the profiles and verified all supplied-goods, administration and narrowly defined hosting traversals after confirmation | The review and relation-decision paths were used; opaque entity-decision validation led to unbound entity creation, so this remains partial workflow acceptance |
| Public decision-and-application workflow | Independently selected Programme, recorded decisions, registered the type and relations, created the school and programmes, and enriched existing profiles; after the recorded contract repairs, committed all three approved directed relationships and retrieved their correct targets | Review, registration, enrichment, paired edge application and typed navigation worked through the public API; intermediate failures and technical restarts remain recorded |

The third trial's Programme definition initially failed an alias collision and
was corrected before saving. The programme records preserved their distinct
2024–2027 and 2025–2028 lifecycles and administrators. No incidental person or
unsupported relationship between the programmes was created. The confirmation
interlocutor supplied semantic feedback, so this trial does not establish
unassisted type selection.

The fourth trial received a 57 KB compact bootstrap with the vocabulary contract
roughly 28 KB into the response. Its terminal display was truncated. The final
bootstrap orders operating instructions before action catalogs and explicitly
teaches recovery from `vocabulary_sync`. Field contents and availability remain
governed by the same active-surface filtering.

The fifth trial first exposed a cold-start review bug: a new cell without a
canonical ledger reported warming and advertised remote maintenance that the
server correctly refused. After repair, it created source-anchored questions,
recorded relation decisions, registered the relation types with canonical
receipts, and verified `supplies`, `administers`, and qualified `hosts`
traversal. The entity decision contract did not explain its exact required
fields or the relationship between the canonical choice and entity name;
those creations used the existing unbound writer. A locally labelled
“validation” request was actually a type-save request and committed as requested;
it was not a server-side dry-run failure.

The sixth trial exposed three public-contract gaps: incomplete type and relation
proposal shapes, an opaque mismatch when a recorded relation definition omitted
an alias added by the proposal, and a single-page question that could not bind
application to a directed pair. The first two were repaired with exact entry
shapes and bounded field-level feedback. The agent corrected its own definitions
and anchored enrichment questions to the existing entities. These corrections did
not relax canonical validation or supply domain choices. The edge gap was repaired
with a current relation-candidate question that reviews both visible endpoint
versions and returns the canonical application route. A further application
refusal exposed a disagreement between the proposal's default `semantic_relation`
origin and the selected-edge validator. Selection now uses the same registry
resolver as canonical authored relations; unrelated-origin and project-scope
refusals remain covered. Intermediate refusals and server restarts remain part
of the trial's evidence.

One restart deliberately disabled the file watcher to isolate graph drain.
Graph synchronization converged, but relation review remained `warming` because
it also requires a live canonical identity census. Both the current main
baseline and the earlier pre-change baseline reproduced this behavior. Restarting
the same cell with the watcher enabled made the queue available without changing
its content or repairing database rows.

The final continuation committed the supplier-to-school edge and both
administrator-to-programme edges. The agent found that the school's summary
mentioned its programme without a wikilink, recorded an enrichment decision,
validated and applied that link, then reviewed and applied the resulting exact
pair. Public typed queries returned the school for supplied goods and the
correct distinct programme for each administrator. The two programme lifecycles,
the existing cooperative identity and the narrowly worded monitoring-visit fact
were preserved.

Receipt readback exposed a final bookkeeping mismatch: expected post-write
currency updated target versions but retained pre-write evidence versions.
The public lifecycle regression now reads the completed review repeatedly and
requires its applied state and canonical receipt. Later endpoint changes retain
the historical decision and receipts with `refresh_required`; they cannot turn
that history into current authority or overwrite an uncertain application.
The preserved trial's older serialized list rows were rebuilt through the existing
projection-maintenance seam. All 47 canonical files and the review ledger were
unchanged; repeated list and exact-context reads then agreed on the historical
receipts and explicit refresh requirement.

## Retrieval limits observed

Exact entity-name queries found the Programme records in the third trial.
Supplier and administrator questions found the later summary/insight notes.
`resolve-entity` reused the existing cooperative alias, but normal and deep
natural-language `ask_memory` for “What do we know about Cedar Co-op?” returned
an empty result. Keyword lookup found the later note after its body explicitly
included the alias.

The alias-query failure was also reproduced against the pre-change baseline.
The lexical index and cue-based referent stage were unchanged by this work.
These trials therefore do not establish full natural-language retrieval
acceptance or semantic-model behavior.

## Rerunnable implementation checks

The vocabulary test modules exercise the protocol and typed decisions,
current-version guards, canonical writer receipts and replay, multi-step relation
application, entity/type reuse, exact and parent-family traversal, provenance
independence, notification deduplication, and recovery.

Resource regressions have explicit adversarial fixtures:

- 1,000 hidden queue items: a default review evaluates 16 candidate items, and
  two passes inspect 32 distinct items while exposing no hidden counts.
- Retained decisions: default review selects actionable work; `state="all"`
  preserves history. Continuations bind the principal and visible decision state.
- 100 independent sessions: transient notices use exact SQLite keys without
  growing the canonical JSON notification map. Expiry cleanup has a fixed batch.
- Large provenance components: indexed root summaries and incremental union
  state avoid repeated component-wide rewrites. Generation retirement uses
  bounded cleanup and rejects stale continuations.
- Bookkeeping outages: an exact replay can reconcile a proven canonical commit
  even when both the initial application update and its uncertainty update failed.

Review decisions remain in the existing canonical review-state owner. Its
whole-file write and point-read cost is an existing limitation; paged vocabulary
review uses a fenced derived SQLite projection. Missing or corrupt derived state
reports warming/unavailable with the operator-only recovery command
`exomem maintain --reconcile`. A new cell with no canonical review ledger
returns a current empty bounded pass without creating a projection.

The ordinary-write comparison is rerunnable with
[`measure_vocabulary_write.py`](../../scripts/measure_vocabulary_write.py).
Use the same Python environment and script for both trees, changing only the
first source directory on `PYTHONPATH`. The script creates disposable vaults
and isolates state, configuration and writer leases. Fixture construction and
one warm-up mutation precede the measured samples. Timings include tracemalloc
instrumentation; they compare the two implementations rather than predict
uninstrumented request latency.

From the candidate checkout, with `BASELINE_TREE` pointing at the clean
pre-change worktree:

```bash
EXOMEM_DISABLE_EMBEDDINGS=1 EXOMEM_DISABLE_MEDIA_EXTRACTION=1 EXOMEM_DISABLE_CLIP=1 \
PYTHONPATH="$BASELINE_TREE/src:$PWD/scripts" \
.venv/bin/python scripts/measure_vocabulary_write.py --label baseline --pages 10 100 --samples 9

EXOMEM_DISABLE_EMBEDDINGS=1 EXOMEM_DISABLE_MEDIA_EXTRACTION=1 EXOMEM_DISABLE_CLIP=1 \
PYTHONPATH="$PWD/src:$PWD/scripts" \
.venv/bin/python scripts/measure_vocabulary_write.py --label candidate --pages 10 100 --samples 9
```

Run these sequentially with other test and benchmark jobs stopped. The output
includes every latency sample, p50/p90, peak memory and instrumented whole-vault
scan counts. Advisory-size and continuation bounds are asserted by the scoped
vocabulary tests separately.

## Existing review decisions

The entity adapter reuses the lifecycle and curation owners shipped in v0.73.0.
Independent overlap review reproduced two integration defects: an originating
family set to off, and an individually dismissed or snoozed candidate, could
remain actionable through the vocabulary queue. Both are corrected. Three
original reproductions and 113 tests across vocabulary entities, review,
notifications, vocabulary state, and review state passed independently.

The tests cover fresh pages and continuations, explicit all-state inspection,
snooze expiry, reopening, changed evidence, and safe refresh of older bindings.
The derived queue composes the existing decisions without changing vocabulary
meaning decisions or reading the full review ledger while serving a page.

## Activation boundary

The v1 workflow retains existing confirmation rules. The v2 implementation
provides external custody records, exact approvals and scoped grants, canonical
effect checks, revocation serialization, receipts, and runtime admission fences.
Its trusted user-control and deployment-floor adapters are unconfigured by
default, so this work does not establish or enable live v2 activation.

Hosted activation also needs durable external authority storage: the current
pod-lifetime, read-only custody mount cannot supply that lifecycle. Mixed
additive and separately controlled rewrite effects remain refused until their
separate approval can be verified. The active OpenSpec change retains those
integration and acceptance tasks.

### Native owner integration

The [local owner workflow](../native-memory-permissions.md) composes the existing
GitHub identity verifier, canonical policy authoring, schema migration,
serving-membership publication and vocabulary authority. Exact previews are
retained in external private control storage. Activation creates no grants;
subsequent grants and exact approvals require separate browser acceptance.

`test_server_owner_startup.py` exercises actual FastMCP composition from owner
login through policy preview and canonical commit, substituting only the
external identity exchange. Missing cookies, agent-only credentials and an
incorrect submission origin cannot approve the review. The native integration
test runs policy, child-process migration preparation, offline migration,
floor publication and activation, then commits a type addition through
`schema_memory` under a grant. Revoking that grant prevents the next addition.

Recovery tests cover expired unstarted reviews, exact interrupted publication,
environment publication after ledger completion, and expiry while waiting for
the activation guard. Packaging tests build a wheel from the source archive,
compare its three service helpers with their canonical sources, and invoke the
installed maintenance module. Runtime tests bind ongoing renewal to the
approved owner, installation and keyring.

These disposable checks do not claim live owner consent or Hosted activation.
Hosted controls belong in the existing Substrate Exomem Home, using its account
session and deployment lifecycle. The native page is registered only for a
configured authenticated standalone server.
