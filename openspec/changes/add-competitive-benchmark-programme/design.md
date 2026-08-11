# Design — competitive benchmark programme

## Governing decisions

1. **Inverted configuration authorship is the fairness answer.** The rescope
   retired comparison because the author configured the competitor. This
   programme makes competitor-side configuration competitor-authored:
   guest lanes run their harnesses with their providers; controlled-direct
   rows wrap their provider classes via a sidecar under their own project
   environment; every remaining knob carries a provenance row (their
   file:line or docs URL) and a knob without provenance refuses to run.
   Residual Exomem-authored glue (projectors, scenario drivers) is size-
   accounted in the fairness matrix, and any comparative publication is
   gated on an independent adversarial review of the same shape that
   produced the 2026-08-08 REJECT.
2. **Lane G (guest) is the primary comparative vehicle; MVP if scope
   collapses = guest lanes + epistemic families 5/7/11/12/14.** Guest rows
   carry borrowed neutrality; the custom suites carry the wedge question.
3. **One reader, one judge, one ledger.** MemoryBench contributes
   checkpointed INGEST/SEARCH artifacts only; ANSWER/EVALUATE re-run in the
   direct lane. Its aggregate (MemScore) and its exclusion of failed
   questions from accuracy are thereby never consumed. bm-bench accuracy
   numbers are likewise re-derived; competitor-harness native numbers are
   never republished as ours.
4. **Equivalence gates.** A committed 25-case LongMemEval-S subset
   (3 answerable × 6 types + 7 abstention, hash-ordered selection, recorded
   dataset sha; changes require an OpenSpec task). Twelve diff keys with
   explicit normalizers; `null` never equals a value. Blocking for
   Exomem-direct vs Exomem-in-MemoryBench; report-mode with mandatory
   written explanation across competitor-authored harnesses (two of their
   harnesses may legitimately disagree — that is a finding, not a block).
   Exceptions register: weaker predicate only, evidence-cited, expiring.
5. **Fail-closed readiness with product-honest statuses.** Positive
   verification only (exit-code-0 evidence rejected): Basic Memory =
   vector-chunk count > 0 + config + log line; Supermemory = terminal
   document status plus a memories-mode canary (status `done` never implies
   extraction; irrecoverable failures auto-delete ≈2 min → `reaped_404`);
   Exomem = doctor checks with refusal-not-degradation. Dynamic-dreaming
   Supermemory gets `readiness-unverifiable` as a disclosed first-class
   status — INVALID would make the product's default mode structurally
   unmeasurable, which is its own bias. Near-zero retrieval is a harness
   fault, never a contender loss.
6. **Variants never collapse** (registry-enforced, disclosure text per row):
   `exomem-{source-only,controlled,native}`,
   `basic-memory-{controlled,native-git,native-nogit}`,
   `supermemory-{hosted-memorybench,hosted-native,local-controlled,local-native,local-documents-v3}`,
   `hybrid-rag-control` (RRF k=60 over BM25 + bge-base — the embedding
   family Exomem and local Supermemory already share), `grep-markdown`,
   `no-memory`. Hosted and local Supermemory run different extraction
   models and are never presented as one product. Both our control and
   MemoryBench's own `rag` provider run — their delta inside one harness
   measures harness/prompt effects.
7. **Epistemic State Bench is a new package importing membench as
   libraries** — membench's oracle is the wrong shape (week-keyed bitemporal
   timelines, not phase-keyed trajectories), its contract forbids new
   cross-product families, and its gates read text while this bench reads
   lifecycle state. One trajectory format whose op vocabulary includes
   out-of-band actions (`external_edit`, `stop_engine`, `fresh_agent`,
   `export`, `snapshot`) so corpus-shaped families (1–9) and operational
   families (10–14) share one runner. Coverage subtraction is executed and
   published: families overlapping public suites report state metrics only.
   Assertions run against a neutral state snapshot produced by per-product
   read-only projectors whose every field mapping cites competitor-authored
   evidence; projector size asymmetry is itself a reportable finding.
   Scoring is five-valued; `not_applicable` (capability-declared) poisons
   the family for comparative claims; a marketed-but-missing property is
   `fail` with the marketing citation; acceptance predicates enumerate ≥2
   structurally different ways to satisfy each invariant (Basic Memory may
   satisfy history retention via VCS in a disclosed `native-git` row).
   Catastrophic integrity failures render `INTEGRITY FAIL` and suppress
   every aggregate. The LLM judge is confined to semantic task success and
   continuation narrative, runs in a final phase (deterministic scores are
   byte-identical without it), and is hard-blocked until the structural
   blinding fix passes a structure-swap test; judge–human agreement (κ)
   precedes any judged number.
8. **Pre-registration.** Scenario families, the assertion registry,
   acceptance predicates, and the strategy decision gates are committed and
   content-hashed before any competitor run; the hash lands in every run
   manifest; later changes are dated amendments. Negative controls
   (ripgrep-over-markdown, no-memory) run in every epistemic table so
   totals are interpretable.
9. **Cost-envelope symmetry.** A local metering proxy fronts every
   provider's model endpoint so Supermemory's server-side extraction tokens
   land in the same envelope as Exomem's write-agent tokens. The budget
   ledger uses reservation semantics (refusal happens on the estimate,
   before the call), a cross-process STOP sentinel, priced models only
   (unknown model refuses), founder-approval records in the ledger, and no
   self-raised caps.
10. **Native and operational lanes stay honest about this machine.** No
    cross-provider latency is publishable from the current host (GPU
    unusable; standing 4b.40 policy) — latency renders indicative-only and
    the report refuses comparative latency columns. Providers run strictly
    sequentially under the RAM budget. Blocked rows (hosted keys, cloud
    modes) render as `blocked: <reason>`, never as losses.
11. **MemoryBench guest rows use exact, persistent, reversible transport
    glue.** The paired rows are deliberately different observations of the
    same products: Basic Memory keeps its own `bm-bench` row and also runs
    its unmodified `BasicMemoryLocalProvider` inside MemoryBench;
    Supermemory keeps MemoryBench's own provider row and receives the
    direct-SDK spot-check in 4.7. Neither pair may collapse into one variant.

    The Basic Memory guest provider talks to one persistent loopback Python
    sidecar across MemoryBench's separate ingest and search processes. The
    sidecar exposes exactly `POST /v1/ingest`, `POST /v1/search`, and
    `POST /v1/cleanup`, under a strict versioned JSON envelope, bearer
    authentication, a 4 MiB body cap, serialized operations, bounded
    deadlines, and byte-identical idempotent replay by request ID. Ingest is
    blocking through positive semantic readiness, so `awaitIndexing` only
    verifies that every requested document has a current receipt for the
    same container. Search forwards the exact MemoryBench limit to the
    competitor class once. A silent embedding fallback, ambiguous non-JSON
    MCP result, over-limit result, missing readiness proof, or stale receipt
    invalidates the row rather than becoming an empty retrieval loss.

    Basic Memory's renderer and provider remain unmodified. Protocol-mandated
    input projection happens before the behavior-preserving observation seam:
    the sidecar replaces the raw MemoryBench question/session ID (including
    `_abs`) with a neutral positional digest before the renderer sees it and
    preserves the private mapping only in evidence. This neutralization is
    dataset hygiene under the canonical-event contract, not a change to a
    competitor-authored renderer or configuration value. The sidecar seeds an
    inert default Basic project whose path is inside the benchmark work root,
    then creates non-default per-container projects; this prevents a fresh
    Basic config from indexing the operator's real `~/basic-memory` and keeps
    final project removal legal. The provider's warm MCP process is observed
    through its isolated Basic log file and a forwarding public-result seam
    that changes neither post-projection arguments, exceptions, nor return
    objects. Every unique ingest/reindex receives fresh fallback detection and
    fresh project/document-specific count evidence before its readiness
    receipt is issued. Only immutable same-process startup/config evidence may
    be reused; a prior session's positive counts can never authorize a later
    session. The unavoidable MemoryBench lifecycle performs one competitor
    ingest and one full reindex per unique session, unlike Basic's grouped
    own-harness row; this directed asymmetry is recorded, not hidden or
    optimized away.

    Cross-process descriptors are published atomically under an exclusive
    launch lock and are accepted only after no-follow ownership/mode checks,
    process-start identity, checkout-pin, and exact work/evidence-root
    binding. Tokens live only in mode-0600 work descriptors; evidence holds
    a token-free projection. The 4.4 transport implements cleanup, but the 4.5
    programme-owned stage runner is its runtime owner because the pinned
    MemoryBench orchestrator never calls `Provider.clear()`: a `finally` path
    and signal handling invoke descriptor-driven cleanup after artifact export
    on success, failure, or interruption, persist cleanup failure, and refuse
    terminal validity while final absence is unproved. Cleanup uses Basic's
    documented project-removal command, proves project/config/corpus absence,
    and calls the public competitor cleanup once when the final namespace
    retires. Owned process groups receive TERM, a five-second drain, then KILL;
    unrelated processes and sibling checkouts are never touched.

    The 4.5 runner emits two strict, versioned contracts. The public
    `memorybench-export.v1.json` is a typed projection for the two 4.4 guest
    providers, `basic-memory` and `exomem`, from the pinned
    checkpoint and canonical per-question result files, not a copy of either
    source envelope. It records the exact harness, dataset, provider and
    variant identities; executed stages `ingest`, `indexing`, and `search`;
    excluded stages `answer`, `evaluate`, and `report`; per-case phase state;
    source-root references and digests; original-order hits projected only as
    finite-score `{content, score}` pairs; stable failure codes; and a closed,
    sorted `missing_fields` vocabulary. Imported timings are always marked
    `publishable: false` with reason `host_unvalidated`. Ground truth remains
    outside the public export in mode-0600 typed private-gold members under a
    mode-0700 directory. An
    unavailable answer-session mapping is represented as null with
    `gold.answer_session_ids` missing, never as an invented empty list.
    Public output references carry safe relative paths. MemoryBench source
    references with evidence-labelled case IDs carry only a domain-separated
    HMAC-SHA256 pseudonym; the exact path lives in private gold and is
    recomputed there by the validator using a random per-run key held only in
    the private run plan. Case and container pseudonyms use separate HMAC
    domains. The exporter never publishes the key or copies, renames, or
    invents a source path to satisfy privacy.

    The closed missing-field vocabulary is
    `question.question_date`, `gold.answer_session_ids`,
    `ingest.transmitted_payloads`, `search.transmitted_query`,
    `search.options.limit`, `search.options.threshold`,
    `search.normalized_hit_ids`, `search.normalized_scores`,
    `search.normalized_ranks`, `search.retry_attempts`, and
    `search.http_status`. The first two are omitted when positively present;
    the remaining entries describe evidence the pinned artifacts do not
    persist. Source-code constants and inference are not evidence. The native
    Supermemory provider's vendor-shaped hits are intentionally outside this
    guest export; 4.7 must ratify its distinct native projection rather than
    force it into the flat guest-hit type.

    Before provider work, an absolute mode-0600 `memorybench-run-plan.v1`
    binds the existing `DatasetIdentity`, exact harness commit/tree/lock,
    provider and registered variant, upstream run ID, output root, guest
    work/evidence roots, and a CSPRNG-generated 32-byte privacy HMAC key. A
    missing or invalid identity is `BLOCKED`; a later
    checkpoint/result disagreement with the valid plan is harness corruption
    and makes the run `INVALID`, never a contender loss or inferred identity.

    The verified dataset is bound to the pinned harness's actual native input,
    not merely to a separate plan file: for LongMemEval the private path is the
    fixed ignored raw-cache path inside a disposable exact-pin MemoryBench
    checkout. Preflight refuses any derived `questions/` cache or existing run
    root; it never downloads, deletes, repairs, or reuses runtime state. The
    pinned adapter creates fresh shards, which are reconciled back to the same
    raw bytes before validity. The private plan binds either the full dataset
    or an explicit ordered question-ID set. A lockfile-pinned additive ingest
    entrypoint passes that exact set through the existing orchestrator
    `questionIds` seam; it never uses MemoryBench's filesystem-order `--limit`
    or random sampling. The 25-case gate uses the committed subset's exact IDs.
    Bun and uv are resolved and verified before
    provider action, Bun is invoked absolutely, and only the verified uv
    directory plus fixed system directories enter the controlled `PATH`.
    This leaves the read-only sibling untouched while proving which bytes the
    contender actually saw.

    Cleanup is a separate `guest-cleanup.v1.json` proof produced by
    `bun run benchmarks/memorybench/cleanup.ts --plan <absolute-mode-0600-json>`.
    The helper attaches through the reviewed secure descriptors and invokes
    each concrete provider's existing `clear(containerTag)` path strictly
    sequentially; it must never launch, repair, or silently replace a missing
    guest during teardown. Its token-free stdout proof carries only keyed
    container pseudonyms, stable outcomes and failure codes, relative evidence references
    and digests, per-namespace absence as applicable, Basic's public cleanup
    call count, aggregate descriptor/process-group/work-root absence, and
    `all_absent`. It never publishes bearer material, raw process identities,
    or absolute paths.

    Cleanup runs in its own process group, observes Basic's zero-or-one public
    cleanup count at the actual finalization seam, and proves process absence separately
    from directory/config absence. Missing directories never fabricate other
    absence surfaces. Target discovery precedes fallible public projection, so
    malformed export evidence cannot erase a namespace that still needs
    teardown. Discovery candidates are validated independently: malformed
    checkpoint, guest-evidence, or descriptor files record their own stable
    partial-export failure while valid siblings remain cleanup targets. All
    JSON sources reject duplicate member names, conflicting
    result sources publish no selected hits, and terminal validity re-reads the
    persisted public artifact through both privacy and artifact validators.

    Cleanup targets are deduplicated and HMAC-pseudonym-sorted from the union of
    non-pending checkpoint container tags,
    container tags in validated guest request/response evidence, and tags in
    secure descriptors. A successful Basic ingest response proves namespace
    existence. A failed or unknown candidate may be classified
    `already_absent` only after deterministic descriptor and work-root absence
    is proven; insecure, missing, or contradictory discovery remains unproved.
    Every discovered target is attempted even after an earlier failure, with
    its own durable outcome; teardown never short-circuits and leaks a later
    namespace.
    The runner writes a started manifest, runs ingest/indexing and search,
    writes a complete or partial export, performs cleanup from one `finally`
    path including handled signals, persists the cleanup proof, and finalizes
    the manifest last. Only a complete export plus `all_absent: true` is
    `VALID`; stage, export, interruption, cleanup, or proof failure is
    `INVALID`. `BLOCKED` is reserved for a prerequisite that fails before any
    provider action. Status and exit are separate: a caught SIGINT/SIGTERM wins
    process-code precedence as 130/143 while its manifest stays `INVALID` and
    retains cleanup failures; otherwise unproved cleanup exits 3, pre-provider
    `BLOCKED` exits 2, `VALID` exits 0, and every other `INVALID` exits 1.

    Draft 2020-12 schemas prove closed structural shape, not arbitrary sibling
    equality, lexical ordering, path containment, or recomputed source facts;
    the standard has no vocabulary for those claims. Every artifact validator
    therefore applies the committed schema, strict model, and shared semantic/
    source recomputation in that order. A schema pass or status field alone can
    never authorize `VALID`. Tests keep separate closed registries for
    deliberately semantic-only schema→model differences and mandatory
    model→artifact source recomputations so neither kind of drift is hidden.

    The Exomem guest provider owns one isolated service per container. It
    runs `exomem init --vault <owned-vault>`, launches with
    `EXOMEM_VAULT_PATH=<owned-vault>` and a random
    `EXOMEM_REST_API_KEY`, authenticates every `/api/*` request, uses an
    ephemeral loopback port, preserves request/idempotency IDs across bounded
    retries, proves hybrid readiness with `exomem doctor`, and tears down only
    its owned process group and work root. Both guest providers declare
    concurrency one for ingest, indexing, search, and cleanup. Wrapper
    timings from this host are non-publishable.

    Registration is an immutable overlay onto the detached pinned
    MemoryBench checkout: the eight §4.4 provider/test files plus the directly
    invoked §4.5 `src/cli/commands/competitive-ingest.ts`, and exactly three
    registration edits (`ProviderName`, provider registry/export, and
    no-key config). `LOCKFILE.json` records every source/destination hash,
    both upstream pins and locks, the three pre/postimages, and the canonical
    binary diff hash. Setup accepts only exact pristine or exact materialized
    states, regenerates the canonical diff, and restores byte-for-byte while
    refusing any unrelated drift. Tests run without Python bytecode/cache
    writes and invoke the pinned local TypeScript compiler directly; no
    package, prompt, harness phase, competitor provider, or lockfile is
    modified.

12. **Epistemic audit claims are reconstructed, never self-attested.** Founder
    ratification leaves the approved `PREREGISTRATION.md` bytes unchanged and
    creates one immutable versioned receipt binding its repository path,
    sha256, decision, ratifier, date, and repository revision. Each later
    amendment has its own immutable, ordered receipt binding the parent digest
    to the amended whole-document digest, its repository revision, the exact
    affected sections, rationale, and effective policy. A comparative run pins
    `contract_revision` and records the complete typed identity chain through
    that revision; neither a plan nor a caller chooses an "applicable" digest.
    Validators re-read and hash every referenced committed artifact as of the
    pin. Later amendments can change current publishability and must be
    disclosed, but never retroactively make a historically valid run's identity
    invalid.

    Privileged-endpoint parity is a closed matrix over exact
    `driver_surface_id × provider × variant`. Both `equivalent` and
    `capability_gap` dispositions require an audit scope, competitor-authored
    evidence citation, and reason; only an equivalent disposition carries a
    competitor surface. The actual Exomem driver inventory comes from a
    persisted, digest-bound invocation receipt emitted by the instrumented
    driver/broker boundary, never from a caller list. Every provider-visible
    credential, socket, SDK, CLI, and filesystem surface is available only
    through that boundary; source/import checks plus runtime capability
    isolation prove the driver cannot bypass it. An undeclared call refuses
    before assertions execute. A proved capability gap becomes a named
    family/table non-comparability exclusion and never a score.

    Deterministic assertion functions remain filesystem-free. The bound-run
    layer persists a typed evidence payload containing scenario/family/phase/
    expectation identity, current and prior snapshot references, probe inputs,
    and the deterministic result. A separate `AssertionEvidenceRef` carries
    its canonical run-relative path and sha256. Every failed assertion requires
    a reference. Validation opens every path component beneath the run root
    without following symlinks, verifies regular-file type and digest,
    reconstructs the assertion context, reruns the frozen assertion, and
    requires exact result equality before a headline can render. Safe path
    syntax is escaped for Markdown output.

    Both products and the exact `grep-markdown` and `no-memory` controls are
    assembled into one `ValidatedEpistemicCohort`. Cohort identity is the exact
    ordered sequence of scenario id+digest, phase, expectation ordinal,
    assertion, subject/counterpart, tolerance, and freshness bound—not a set of
    family names. Every control×scenario row contains at least one actual
    deterministic `pass` or `fail`; empty and blocked/unsupported/N/A-only rows
    refuse. When either control passes an assertion instance, the matching
    product result remains the five-valued `pass` and receives the orthogonal
    `signal_disposition=no_product_signal`; G2 excludes masked instances and
    the report discloses them. The single public epistemic table renderer
    consumes only this validated cohort, so report generation cannot bypass
    the gates.

13. **LongMemEval-S comparative selection is a frozen source-derived artifact.**
    The repository owns `benchmarks/equivalence/subsets/lme-s-25.json`, a
    closed `lme-selection.v1` record generated only from
    `xiaowu0162/longmemeval-cleaned` revision
    `98d7416c24c778c2fee6e6f3006e7a073259d48f`, filename
    `longmemeval_s_cleaned.json`, SHA-256
    `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`,
    277383467 bytes, 500 rows, and its six-type census. It spells the ordered
    type tuple `single-session-user`, `single-session-assistant`,
    `single-session-preference`, `multi-session`, `temporal-reasoning`,
    `knowledge-update`. Every raw row has a known type before routing;
    abstention is precisely an ID ending `_abs`. The selector computes
    SHA-256 of UTF-8 `question_id + dataset_sha256`, orders by
    `(digest_hex, question_id)`, takes three non-abstentions per type in that
    order, then seven abstentions. Missing strata, malformed identities,
    duplicate/blank IDs, altered source facts, or a non-identical regeneration
    refuse before reader or provider construction. Direct canonical mode and
    MemoryBench's explicit 25-case plan each re-derive the repository artifact,
    require exact ordered IDs, and persist its relative path, SHA-256, and
    algorithm version in manifest evidence. Generic `--pilot 25` remains a
    separate non-comparative selection and cannot substitute for this cohort.

14. **Direct-provider lifecycle evidence is runner-owned.**  Registration
    resolves an inert immutable provider specification before the started
    manifest; construction and setup happen afterwards in a run-owned session
    context.  The requested registry key and observed variant remain distinct.
    Every constructed diagnostic or scored instance has one outer lifecycle
    owner, which calls cleanup exactly once, persists only raw independently
    re-observed cleanup facts, and terminalizes failures without replacing a
    control-flow exception.  Direct traces use v2 records, each self-versioned
    and bound to a contained cleanup-observation reference.  VALID and
    READINESS_UNVERIFIABLE terminalization and later report loading require
    exact expected-instance trace/observation completeness; a one-case run is
    isolation-unverifiable because it has no prior live session to probe.

## Execution

fable-delegate discipline: Stage-0 packet → Codex/Claude implementer lanes in
isolated worktrees with red-first evidence → fresh independent reviewer over
the actual diff → orchestrator acceptance quoting verdict and diff stat.
Routing per the repository Codex protocol: mechanical, well-specified modules
to Codex lanes; design-sensitive pieces (TypeScript providers, Supermemory
adapter, assertion engine) to Claude executors; adversarial reviews to Sol
xhigh read-only; OpenSpec/docs/KB writes Claude-side. Concurrency capped;
benchmark runs only on a quiesced machine. The red leak test opens the first
implementation lane (observed failing before the normalizer exists).

## Alternatives rejected

- **Keep the retirement absolute** (no competitor rows ever): answers no
  product question; the marginal cost of testing the wedge is now lower
  than the cost of not knowing (rescope already concedes the suite-format
  argument).
- **Extend membench into the epistemic bench**: schema changes would re-pin
  the frozen corpus release, violate the internal-instrument contract, and
  inherit text-reading gates blind to lifecycle state (the 4b.43 lesson).
- **Hash whichever pre-registration file is present at run time**: loses the
  original founder decision and lets a run silently select amendments. The
  ordered receipt chain is the authority.
- **Trust a driver-supplied endpoint list**: a privileged call can be omitted
  from the claim. Invocation inventory must be emitted and hashed at the
  instrumented boundary.
- **Put only a path string on a failed assertion**: a safe path may still point
  to unrelated bytes. The validator must re-read the typed evidence bundle and
  reproduce the deterministic result.
- **Compare controls by family-name sets**: equal names can hide different
  scenarios, parameters, phases, or multiplicity. Cohorts bind the exact
  ordered assertion instances instead.
- **Vendor MemoryBench into this repo**: MIT permits it, but it invites
  drift-by-local-edit and bloats the tree; a pinned sibling checkout with a
  lockfile, hash-verified provider sync, and a single registration patch is
  strictly more auditable.
- **Author competitor adapters directly** (the pre-audit approach): the
  defect class that voided every prior result; excluded by requirement.
