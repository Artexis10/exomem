## ADDED Requirements

### Requirement: LME CPU Profile Is Enforced Across Both Execution Paths

The Exomem LongMemEval direct adapter and MemoryBench guest SHALL enforce the
same canonical CPU profile before model initialization and doctor execution.
The profile SHALL override ambient performance mode, global and text/CLIP
device overrides, and CUDA visibility without changing the operator's config.
Embeddings and CLIP SHALL remain enabled. The export stage SHALL carry the
canonical profile through its controlled child environment. Direct run evidence
and guest provider evidence SHALL record the requested profile explicitly;
declared settings SHALL NOT be presented as observed model-device measurements.
A changed profile requires fresh direct and guest runs at the new provider pin.

#### Scenario: Ambient GPU settings cannot change the benchmark device
- **GIVEN** an operator selects performance mode and CUDA or MPS model overrides
- **WHEN** either Exomem benchmark path initializes its models
- **THEN** text and CLIP select CPU without accelerator probing
- **AND** the child environment hides CUDA devices, including during doctor
- **AND** the operator's persistent configuration remains unchanged

#### Scenario: Export and transport retain the same declared CPU policy
- **WHEN** the export creates its controlled environment and the guest launches
- **THEN** both apply the canonical direct profile, including empty-valued pins
- **AND** evidence identifies these values as requested configuration

### Requirement: Canonical Events Quarantine Gold
Provider adapters SHALL receive dataset content only as canonical protocol
events carrying neutralized public identity (ordinal session identity, a
content hash over provider-visible bytes, declared timestamp semantics, and
hashed upstream identifiers). Gold answers, evidence labels, category labels
that leak answers, and future question text SHALL live in a separate record
type that adapter interfaces structurally cannot receive.

#### Scenario: Adapter cannot receive gold
- **WHEN** benchmark code attempts to pass a gold-bearing case record where
  an adapter expects protocol events
- **THEN** the call fails at the interface, and a test asserts this failure

#### Scenario: Evidence-labelled upstream identifiers are neutralized
- **WHEN** a dataset's raw session identifiers carry answer-evidence markers
- **THEN** normalized events expose only ordinal identity and a hash, and a
  dataset-level precondition refuses ingestion under an outdated neutralizer

### Requirement: Outbound Payloads Are Scanned For Leakage
The substrate SHALL scan the payloads actually transmitted to providers —
captured at the transport layer, not the intended payloads — under
scope-specific policies: ingestion payloads strictly (any gold text, answer
shingle, label token, or evidence-marked identifier invalidates the case),
search payloads advisorily (question text is legitimate), and run artifacts
permissively (gold is required for judging).

#### Scenario: Ingest leak invalidates
- **WHEN** an ingestion payload contains gold answer text, an answer
  shingle, a category label, or an evidence-marked identifier
- **THEN** the case is INVALID and unscored, and the manifest records the
  detector that fired

### Requirement: Case Namespaces Are Isolated And Canary-Probed
Every case SHALL run in a fresh provider namespace recorded in its
artifacts, verified by deterministic canaries: a presence canary planted in
non-evidence content must be retrievable within the case, a foreign case's
canary must not be retrievable, and a never-ingested canary must not be
retrievable. Contamination invalidates the case; unverifiable isolation is
recorded and blocks cross-provider tables.

#### Scenario: Cross-case bleed detected
- **WHEN** a canary planted for one case is retrieved inside another case's
  namespace
- **THEN** the affected case is INVALID and the run records contamination

#### Scenario: Isolation diagnostics cannot alter scored retrieval
- **WHEN** the runner probes a scored case's namespace
- **THEN** it first retrieves and reads the unchanged dataset corpus, then
  ingests the separately scanned presence canary into that same namespace
- **AND** no diagnostic document can displace a dataset hit or enter reader
  context, including when the corpus contains fewer documents than top-k
- **AND** the trace records each presence, foreign-case and never-ingested
  probe's query, limit, returned hit identities and content digests, and which
  returned hits contain the exact token; absence of a prior case is explicit
- **AND** failed presence remains unverifiable, never an inferred pass

### Requirement: Readiness Fails Closed With Positive Verification
A run SHALL verify every requested retrieval lane (lexical, semantic,
reranker) by positive evidence — index counts, configuration state, or a
passing zero-lexical-overlap semantic probe — before scoring. Command exit
codes SHALL NOT count as readiness evidence. A requested-but-unverified lane
or a detected silent fallback renders the run INVALID. Where a provider's
default mode offers no completion signal for derived state, the run SHALL be
labelled readiness-unverifiable on every affected row rather than scored as
verified or discarded as invalid.

#### Scenario: Silent semantic fallback invalidates
- **WHEN** semantic retrieval was requested and the provider served
  keyword-only results without error
- **THEN** the run is INVALID with the fallback recorded

### Requirement: Manifests And Traces Make Reports Regenerable Offline
A machine-readable manifest SHALL be written before the first provider call
and finalized with a terminal validity status; per-case traces SHALL persist
normalized inputs, transmitted payload digests, queries, raw responses,
normalized results, packed context, prompts, model identities, judge
outputs, timings, token and cost accounting, and cleanup results. Report
generation SHALL read only stored artifacts, refuse non-terminal manifests
and unknown schema versions, and prove offline operation via a network
guard. Every comparative run/result manifest SHALL contain a typed
pre-registration identity derived from the checked-in founder-ratification
receipt and its complete ordered amendment-receipt chain through the manifest's
pinned `contract_revision`. Every amendment receipt SHALL bind its own
repository revision. Artifact validation SHALL re-read and hash every
referenced receipt and contract artifact as of the pin and refuse a missing,
caller-substituted, incomplete, out-of-order, or repository-inconsistent
identity before report generation. Later amendments SHALL affect current
publishability and disclosure, not the historical validity of a run whose
pinned chain remains complete.

#### Scenario: Report from artifacts only
- **WHEN** a report is regenerated from a completed run directory with the
  network guard active
- **THEN** rendering succeeds with zero provider or network calls

#### Scenario: Caller-selected pre-registration identity is refused
- **WHEN** a plan, manifest, or result substitutes a digest or omits an ordered
  amendment receipt effective at its pinned contract revision
- **THEN** validation refuses before the artifact can support a comparative
  report

### Requirement: Spend Is Reserved Before It Happens
Billable operations SHALL reserve an upper-bound estimate against a shared
ledger before the call; a reservation that would exceed the approved cap is
refused before any spend occurs, a stop sentinel halts all processes, an
unpriced model or operation refuses rather than estimating zero, founder
approvals are recorded in the ledger, and no run can raise its own cap.

#### Scenario: Cap refusal precedes the call
- **WHEN** a billable call's estimate would exceed the remaining approved
  budget
- **THEN** the call is refused before transmission and the ledger records
  the refusal

### Requirement: Versioned Guest Transports Fail Closed
Guest transports SHALL use a strict versioned request and response contract,
authenticate loopback calls, reject unknown or duplicate fields, non-finite
numbers, unsupported content types, oversized bodies, request-ID collisions,
and ambiguous competitor responses, and SHALL bound startup, operations,
retries, and owned-process teardown. Stable transport failures SHALL produce
safe evidence without tokens, tracebacks, operator paths, or unrelated
environment values. A transport or environment failure SHALL invalidate the
row rather than synthesize a miss or product loss.

#### Scenario: Ambiguous competitor response is not scored
- **WHEN** a wrapped competitor returns guidance or bytes that its public
  provider cannot unambiguously parse as the declared result contract
- **THEN** the guest transport records an environment fault and the row is
  INVALID rather than an empty successful search

### Requirement: Blocking Ingest May Carry Positive Readiness
A guest transport MAY block ingestion until requested derived state is
positively verified and return a versioned readiness receipt. In that form,
`awaitIndexing` SHALL make no second readiness request; it SHALL verify that
every requested document is covered by a current, verified receipt for the
same provider namespace and requested lane. Missing, stale, cross-namespace,
or fallback-tainted receipts SHALL refuse scoring.
For a provider that rebuilds derived state on every unique ingest, each receipt
SHALL carry fresh fallback detection and fresh project/document-specific state
evidence from that ingest; only immutable startup/config evidence from the
same still-live process MAY be reused.

#### Scenario: Receipt coverage is incomplete
- **WHEN** `awaitIndexing` receives document IDs for which the blocking ingest
  produced no current positive receipt in that container
- **THEN** indexing fails closed and progress is not reported as complete

### Requirement: Guest State Survives Stage Process Boundaries
Guest providers whose harness runs ingest and search as separate operating
system processes SHALL persist only the minimum reconnectable service state
in an atomically published, mode-0600 descriptor. Attachment SHALL verify a
no-follow regular file owned by the current user, an exclusive launch
identity, process start identity, provider and checkout pins, and exact work
and evidence roots. Live mismatches, symlinks, stale PIDs, and concurrent
double launches SHALL be refused rather than replaced.

#### Scenario: Search reattaches to ingest service
- **WHEN** the MemoryBench search process starts after the ingest process has
  exited
- **THEN** it attaches only to the matching live descriptor and reuses the
  same warm provider state without exposing its authentication token

### Requirement: Cleanup Proves Namespace Absence
Guest cleanup SHALL use the product's documented lifecycle operation where
one exists, delete only benchmark-owned state, prove namespace, corpus,
configuration, process, and work-path absence as applicable, and persist any
cleanup failure. Shared provider cleanup SHALL run exactly once after the
final live namespace, and a response-triggered service shutdown SHALL begin
only after that response is flushed. When the upstream harness has no cleanup
hook, the programme-owned outer stage runner SHALL invoke descriptor-driven
cleanup in a `finally` path and on handled termination signals after artifact
export. Cleanup failure SHALL be persisted, and terminal validity SHALL be
refused while final absence remains unproved.

#### Scenario: Intermediate namespace cleanup preserves shared service
- **WHEN** one Basic Memory namespace is cleared while another remains live
- **THEN** only the named project and corpus are removed, absence is proven,
  and the shared warm MCP process is retained

#### Scenario: Harness omits provider cleanup
- **WHEN** ingest or search succeeds, fails, or is interrupted under a pinned
  harness that never invokes `Provider.clear()`
- **THEN** the programme-owned runner calls the transport cleanup, records its
  result, and refuses terminal validity unless final absence is proven

### Requirement: Guest Service Residency Is Bounded Across Stages
The Exomem MemoryBench guest transport SHALL cap live owned services across the
whole staged run at `MEMORYBENCH_EXOMEM_MAX_LIVE_SERVICES`, a positive integer
defaulting to one.  Admission SHALL be serialized across stage processes.  When
a new container would exceed the cap, the least-recently-used live service
SHALL first reconcile its derived state and require `graph_status` to be
`current|refreshed`, then retire it and prove its owned process group and secure
descriptor absent before a replacement is spawned.
`GRAPH_SYNC_STABILIZATION_EXHAUSTED` with `graph_status: unavailable` MAY retry
the reconcile, with fresh attempt identities and at most three stabilization
failures. Exact `graph_status: unavailable`, `graph_sync: pending`,
`GRAPH_SYNC_REBUILD_IN_PROGRESS` and a valid checkpoint digest MAY wait five
seconds before another reconciliation. The entire barrier SHALL have a
120-second budget, cap each subprocess at the lesser of its 70-second deadline
and remaining budget, and never launch a live out-of-process index drain.
Every other non-current response and exhaustion of either bound
SHALL fail admission and clear all live services.  Residency retirement MAY
preserve the owned vault and work root needed by a later stage; terminal
container cleanup SHALL additionally prove owned guest-process and work-root
absence.  A completed indexing step SHALL retire its now-idle service, and a
search attempt SHALL clear its finished container on both success and failure.

The retirement reconcile SHALL invoke the pinned checkout's local
`exomem maintain --reconcile --json` command, with the owned vault and machine
state bound through the child environment and the canonical CPU profile.
It SHALL NOT request write-mode maintenance through REST. Each attempt SHALL
record its identity, local-CLI transport, process exit and parsed response;
a nonzero process exit or unsuccessful envelope SHALL fail admission.

Ingest, indexing, and search exceptions SHALL clear every live service owned by
that stage instance before the exception escapes.  SIGINT, SIGTERM, uncaught
exceptions, and unhandled rejections SHALL trigger one idempotent cleanup of
every securely discovered live Exomem service, wait for any retirement already
in flight, prove absence, and then re-raise the original signal or failure.
Clearing an absent container SHALL inspect deterministic descriptor and work
roots without calling the service-creation path.

#### Scenario: Five sequential containers respect the default cap
- **WHEN** at least five distinct container tags are admitted sequentially,
  including across separate stage processes
- **THEN** no observation contains more than one live owned Exomem service and
each replacement starts only after the prior process group and descriptor are
absent

#### Scenario: Transient graph stabilization exhaustion is bounded
- **WHEN** residency reconciliation returns exact
  `GRAPH_SYNC_STABILIZATION_EXHAUSTED` before a later attempt proves current
- **THEN** the provider retries with fresh attempt identities up to three total
  stabilization failures, retires only after proof, and fails closed if the bound is exhausted

#### Scenario: A registered graph rebuild is pending at retirement
- **WHEN** reconciliation reports the exact registered-rebuild pending state
  with a valid checkpoint digest
- **THEN** the provider waits and rechecks within the shared 120-second budget
- **AND** only an observed current or refreshed graph permits retirement;
  queued repair, malformed state and deadline exhaustion refuse admission

#### Scenario: Indexing and search retire as they go
- **WHEN** indexing completes for a container or search returns or throws
- **THEN** indexing preserves the container corpus but retires its live service,
  while search performs terminal cleanup and proves the work root absent

#### Scenario: Mid-run failure cannot strand services
- **WHEN** a question operation, indexing step, or whole run throws
- **THEN** every live service owned or securely discovered by that process is
  retired with absence proved before the original failure escapes

#### Scenario: Termination keeps honest process status
- **WHEN** SIGINT or SIGTERM arrives while services are live or retirement is
  already in flight
- **THEN** cleanup runs once without deadlock, every live service is proved
  absent, and the original signal is re-raised

#### Scenario: Clearing an absent container is attach-only
- **WHEN** `ExomemProvider.clear(containerTag)` is called with no live or
  persisted service for that tag
- **THEN** teardown proves deterministic absence without spawning a service

### Requirement: Direct Provider Lifecycle Is Independently Observable
Direct-provider registration SHALL expose an inert immutable specification with
a factory, static descriptor, namespace derivation, and runner-owned runtime
binding. The runner SHALL distinguish requested provider from observed variant,
bind the latter only after setup, and refuse drift. Each constructed provider
instance SHALL receive an immutable run/session/namespace/work/evidence
context and SHALL have exactly one cleanup-owning outer lifecycle.

The static descriptor SHALL declare either an in-process,
no-post-return-background execution model or an
owned-subprocess-terminated-at-cleanup execution model. Unknown or
background-capable declarations, unsafe direct run IDs, and unsupported
descriptor custody SHALL refuse before factory invocation. On supported POSIX/Linux filesystems the
runner SHALL functionally prove required descriptor-relative operations and
`/proc/<runner-pid>/fd/<fd>` reopening on the actual output filesystem, and
SHALL hold separate session, work, and evidence directory capabilities before
factory invocation. Providers SHALL receive capability paths plus canonical
logical refs; capability paths SHALL NOT be serialized. Every authority
operation SHALL revalidate the parent/name/child inode binding. Binding loss
SHALL force INVALID, continue only on the held inode, and SHALL NOT touch a
replacement installed at the original name.

Provider-visible entries SHALL be atomically moved into a random mode-0700
runner-private quarantine and identity-checked. Under the declared in-process,
no-post-return-background execution model, final removal of an empty private
quarantine container is outside the adversarial provider boundary. This
requirement SHALL NOT claim resistance to arbitrary unrelated same-UID actors
that guess or replace that private name; that threat requires process isolation
or an amendment. Original, provider-exposed, and public names SHALL still
survive replacement or fail closed.

A row declaring the owned-subprocess-terminated-at-cleanup execution model MAY
own exactly one sidecar process, started in its own process group and reached
over loopback with a per-session bearer token. Every provider method SHALL be a
blocking round trip, and `cleanup()` SHALL terminate and reap the entire owned
process group before returning. The runner-owned runtime binding for such a row
SHALL additionally observe a `process-group` surface, and absence SHALL require
both zero live owned processes and an unbound listener. Raw process IDs, ports,
and bearer tokens SHALL NOT be serialized into cleanup evidence. The quarantine
caveat above is scoped to the in-process model and SHALL NOT be read as
extending to this one.

The guest transport SHALL inspect every decoded response before retry,
response evidence or provider return. The actual service bearer token and its
UTF-8 base64, base64url and hexadecimal encodings SHALL refuse in keys, values
or bounded nested JSON with a constant diagnostic and normal failure cleanup.
This check SHALL run while the transport owns the credential; export SHALL NOT
depend on descriptors that normal retirement has already deleted.

#### Scenario: Sidecar row proves process absence
- **WHEN** a provider declaring owned-subprocess-terminated-at-cleanup returns
  from `cleanup()` while an owned sidecar process survives or its loopback
  listener still accepts
- **THEN** the runner SHALL derive absence as unproved from the `process-group`
  surface and SHALL terminalize the run INVALID rather than retire the work
  inode

#### Scenario: Sidecar row cannot borrow the in-process caveat
- **WHEN** a subprocess-backed row declares the in-process,
  no-post-return-background execution model
- **THEN** registration SHALL refuse before factory invocation rather than
  admit an unevidenced execution-model claim

Cleanup observations SHALL be self-identifying raw facts only, written under
the session evidence root and independently reopened, digest-checked, and
re-observed by the runner. Provider-returned cleanup claims SHALL not establish
absence. The accepted order SHALL be cleanup exactly once; complete normalized
raw observation and independent absence derivation; exclusive bounded publish;
same-fd bounded reopen, schema, exact-byte and digest validation; live
post-publication re-observation with exact equality and independent absence;
all custody binding checks; expected-instance and cleanup-trace registration;
then bounded descriptor-relative runner retirement of the work inode. Provider
residue SHALL remain in the raw observation and invalidate before retirement.
A direct trace SHALL use self-versioned case-trace v2 records; its
cleanup record binds only session identity and the contained observation
reference. VALID and READINESS_UNVERIFIABLE terminalization and later artifact
loading SHALL require exactly one validated cleanup record and observation for
each factory-returned instance, with no downgrade, duplicate, or orphan. Direct
v2 trace filenames and filesystem roots SHALL use an injective runner-generated
internal session ID; raw question IDs SHALL remain logical attempt metadata.
Terminal validation SHALL enforce the exact bijection among factory-returned
attempts, expected instances, v2 trace filenames, exactly-one cleanup rows, and
reopened observations, binding filename/session, run, namespace, requested
provider, manifest/environment/observed variant, observation path, and digest.
Later validation SHALL inventory every trace/evidence entry through bounded
no-follow traversal and refuse unsafe names, unknown suffixes, nonregulars,
empty or oversized files, invalid or mixed rows, and orphans.

Later loading SHALL inventory and validate every present direct marker and
artifact. Coherent erasure or rewrite of every direct discriminator, v2 trace,
and cleanup observation can be byte-identical to a genuine legacy run and is
outside this frozen v2 claim. Stronger direct-versus-legacy provenance requires
a versioned manifest discriminator and, against hostile same-UID rewrite, an
authenticated external commitment; this requirement introduces neither.

This capability boundary SHALL cover only direct-provider session work/evidence
roots and lifecycle-authorizing v2 traces/cleanup observations. The
caller-supplied run directory remains the trust anchor for each command.
Ordinary manifest, environment, dataset, budget, and report I/O retains its
existing contract and is not claimed race-proof against same-UID replacement of
the whole run directory. Later load SHALL begin a fresh no-follow traversal from
the supplied run directory and SHALL NOT claim cross-command directory
authenticity.

#### Scenario: One live session cannot establish cross-case isolation
- **WHEN** a direct run contains one scored case
- **THEN** it records typed `not-applicable-no-prior-case` and terminalizes
  READINESS_UNVERIFIABLE or INVALID rather than VALID

#### Scenario: Cleanup artifact disagreement invalidates
- **WHEN** a cleanup observation is missing, unsafe, changed, duplicated,
  unobservable, or disagrees with independent re-observation
- **THEN** absence is unproved and the run terminalizes INVALID

#### Scenario: Held lifecycle root loses its name binding
- **WHEN** a provider or concurrent actor renames a held session, work, or
  evidence root and installs a replacement at the original name
- **THEN** the lifecycle terminalizes INVALID, any bounded retirement remains
  descriptor-relative to the held inode, and the replacement is untouched

#### Scenario: Trace topology contains an unsafe or orphan entry
- **WHEN** terminalization or later loading inventories a renamed, swapped,
  cleanup-free, empty, duplicate-cleanup, orphan, symlink, FIFO, nonregular,
  oversized, mixed-version, invalid-row, unknown-suffix, or unsafe-name trace
- **THEN** lifecycle completeness is refused without following or blocking on
  the entry

### Requirement: MemoryBench Export And Cleanup Wires Are Strict
The programme-owned MemoryBench runner SHALL emit a strict
`memorybench-export.v1.json` projection and a strict `guest-cleanup.v1.json`
proof for the two 4.4 guest providers `basic-memory` and `exomem`. The native
Supermemory provider's vendor-shaped hit envelope is not part of this wire;
4.7 SHALL ratify its distinct projection before a native Supermemory row is
exported. Every object in these contracts and their private inputs SHALL reject
unknown fields. Every digest SHALL match lowercase `[0-9a-f]{64}`. Every
artifact reference SHALL contain exactly `root`, nullable `path`, nullable
`path_hmac_sha256`, and `sha256`, where `root` is `memorybench_run|output` and
`sha256` is the referenced file digest. The record is a closed discriminated
union: `output` requires a non-empty safe relative POSIX `path` and
`path_hmac_sha256: null`; `memorybench_run` requires `path: null` and the
domain-separated HMAC-SHA256 pseudonym of the exact private relative path in
`path_hmac_sha256`. A safe relative
path has no absolute form, backslash, empty segment, `.` segment, or `..`
segment. The roots resolve only as
`memorybench_run = memorybench_home/data/runs/upstream_run_id` and
`output = output_root`; references never use implicit precedence or copy,
rename, or invent a protected source path to change roots.

Before provider work, the runner SHALL accept only an absolute no-follow
regular `memorybench-run-plan.v1` file owned by the current user, mode 0600,
whose parent is not group/world writable. Its closed fields SHALL be:

- literals `protocol_version: "1.0.0"`, `schema_version: 1`, and
  `artifact_type: "memorybench-run-plan.v1"`;
- `run_id` and `upstream_run_id` matching
  `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`; `provider` from
  `basic-memory|exomem`; a non-empty registered `provider_variant`; and
  literal `benchmark: "longmemeval"`;
- closed `selection` as either `{mode:"full",target_question_ids:null}` or
  `{mode:"explicit",target_question_ids:[...]}`, where the explicit list is
  non-empty, unique, contains only exact raw dataset question IDs, and fixes
  both the selected set and public case order before provider work;
- `harness` containing repository
  `https://github.com/supermemoryai/memorybench`, commit
  `118209a746d97d0d85e5a7234267f0b6962857e9`, tree
  `2ee25bdbcb6bfaaecb32f917920c53775a299b37`, and Bun-lock SHA-256
  `561d761fd16f895a6597227c6fc1e46064779284317fd479e079e3c86b9857da`;
- the existing six-field `DatasetIdentity` plus an absolute no-follow regular
  `dataset_path` whose recomputed bytes and decoded case count match it;
- `provider_checkout` containing an absolute exact checkout root, public
  repository identifier, verified 40-hex commit/tree, and 64-hex lock digest;
- absolute `memorybench_home` verified as that exact detached materialized
  checkout, an absolute new `output_root`, and disjoint `guest_work_root` and
  `guest_evidence_root` strictly contained by the output root; and
- the 64-hex pre-registration digest; and
- `privacy_hmac_key_hex`, exactly 32 CSPRNG-generated bytes encoded as 64
  lowercase hex characters, present only in this mode-0600 plan and never in
  public export, private gold, cleanup proof, stdout/stderr, argv, provider
  environment, or guest evidence.

For `longmemeval`, `dataset_path` SHALL be exactly
`memorybench_home/data/benchmarks/longmemeval/datasets/longmemeval_s_cleaned.json`.
Every path component below the verified checkout root SHALL be traversed
without following symlinks, and the file's bytes and decoded case count SHALL
be reverified immediately before provider work. The native derived cache
`memorybench_home/data/benchmarks/longmemeval/datasets/questions` and the native
run root `memorybench_home/data/runs/upstream_run_id` SHALL both be absent at
preflight; the runner SHALL refuse stale or pre-existing runtime state rather
than delete, repair, reuse, download, or silently replace it. Before terminal
`VALID`, the raw dataset bytes SHALL still match the plan and the question
shards created by the pinned harness SHALL be no-follow regular files whose
exact case set and decoded values reconcile with the pinned adapter's
deterministic split of those raw bytes. The benchmark checkout used for a run
is disposable run custody; the read-only sibling checkout is never a runtime
workspace.

The exact overlay SHALL include one additive, lockfile-hashed ingest entrypoint
that securely re-reads the same run plan and its runner-supplied digest, rejects
duplicate members, and calls the pinned orchestrator's existing `ingest` path
with no selection argument for `full` or with exactly the plan's explicit
`questionIds`. It SHALL NOT use MemoryBench `--limit` or random sampling. The
runner's first stage SHALL invoke that entrypoint with the absolute verified
Bun executable, plan path, and plan digest; search SHALL then use the pinned
native search command for the same upstream run ID. For an explicit selection,
checkpoint `targetQuestionIds` SHALL equal the plan list exactly. For `full`,
an absent checkpoint target list is accepted only with the exact full dataset
case set. Export completeness is computed against the plan selection, never a
population inferred from post-provider checkpoint/results. The blocking
25-case gate SHALL use the committed `lme-s-25.json` IDs as an explicit
selection after ledger 2.5-residual lands; no `-l 25` shortcut is permitted.

Missing, invalid, or mismatched plan identity SHALL be pre-provider `BLOCKED`.
A missing, insecure, symlinked, ambiguously encoded, or duplicate-member run
plan SHALL be handled inside that same quiet refusal boundary: exit 2, no
traceback, absolute path, or exception body. Before provider work the runner
SHALL resolve Bun and uv from the operator environment to absolute executable
files, verify Bun `1.3.14`, and refuse unavailable or invalid tools. Provider
stages and cleanup SHALL invoke the resolved Bun executable directly; their
controlled environment SHALL contain only the verified uv directory plus
required fixed system executable directories in `PATH`, never `os.defpath`
alone and never the ambient `PATH` wholesale.
A later checkpoint or result disagreement with that valid plan SHALL instead
be `INVALID` harness corruption, never an inferred identity or contender loss.
`DatasetIdentity.source` SHALL be a canonical public HTTPS URI or registry
identifier. Absolute POSIX/Windows paths, backslashes, local/file URIs, and
`.` or `..` path segments SHALL be refused; the absolute `dataset_path` stays
private and never enters the export.

The public export's closed top-level fields SHALL be:

- literals `protocol_version: "1.0.0"`, `schema_version: 1`, and
  `artifact_type: "memorybench-export.v1"`;
- `status: complete|partial`, the plan's run/provider/provider-variant/
  benchmark, exact `harness` and `dataset`, constant
  `executed_stages: [ingest,indexing,search]`, and constant
  `excluded_stages: [answer,evaluate,report]`;
- `privacy` fixed to `classification: provider_safe_reader_input`,
  `contains_ground_truth: false`, and
  `source_results_contain_ground_truth: true`;
- `latency` fixed to `publishable: false` and `reason: host_unvalidated`;
- sorted-unique top-level stable `failure_codes`; and
- non-empty `cases`, unique and sorted by ascending `case_ordinal`.

Each public case's closed fields SHALL be `case_ordinal` (one-based),
`case_id_hmac_sha256`, public `question` with `text`, `type`, and nullable `date`,
nullable `container_tag_hmac_sha256`, a nullable checkpoint artifact reference, a
nullable canonical result artifact reference, a nullable private-gold artifact reference,
`phases`, original-order `hits`, sorted-unique stable `failure_codes`, and
sorted-unique `missing_fields`. `phases` SHALL contain exactly `ingest`,
`indexing`, and `search`; each contains `status` from
`unobserved|pending|in_progress|completed|failed` and a nullable stable
`failure_code`; `unobserved` is required when no validated checkpoint proves
the upstream state.
Each hit SHALL contain exactly non-empty `content` and a finite numeric
`score`. A complete export requires every case and phase complete, a canonical
result and private-gold reference for every case, no failure code, and the
exact plan dataset case set. `partial` SHALL never claim missing cases or
phases complete.

Each referenced private-gold member SHALL reject unknown fields and contain
exactly literals `protocol_version: "1.0.0"`, `schema_version: 1`, and
`artifact_type: "memorybench-private-gold.v1"`; `case_id_hmac_sha256`; raw upstream
`question_id` and `container_tag`; `question`, `question_type`, and
canonical source `ground_truth` as `string|integer`; original-order unique `answer_session_ids` as
`string[]|null`; the checkpoint and
canonical-result source pairs as `checkpoint_path`/`checkpoint_sha256` and
`canonical_result_path`/`canonical_result_sha256`; and sorted-unique private `missing_fields`
restricted to `gold.answer_session_ids`. The parent directory SHALL be mode
0700 and every member mode 0600. An unavailable answer-session mapping SHALL
be null and missing, never an invented empty list.
For every public `memorybench_run` reference, the private member's matching
path SHALL recompute to `path_hmac_sha256`, resolve under the exact MemoryBench run root,
and recompute the referenced bytes to `sha256`. The private-gold public
reference itself uses `root: output` and an HMAC-derived safe filename, so no
raw question/container identity occurs in public bytes.

All public pseudonyms SHALL use HMAC-SHA256 with exact bytes:
`HMAC-SHA256(hex_decode(privacy_hmac_key_hex), UTF8(domain) || 0x00 ||
UTF8(raw_value))`, where domain is exactly `case-id`, `container-tag`, or
`artifact-path` for the correspondingly named field. Raw values receive no
Unicode normalization; HMAC output is 64 lowercase hexadecimal characters.
With key
`000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f`,
the cross-language test vectors are: case `q-01_abs` →
`94e872ad0278c5e760d5ff4a7f170e513c148711365fc3d72bc45b12fc90f131`;
container `q-01-run-01` →
`97b7ccef0e2c66cba51712bac76a50a19832e709b9534d1677d0872342e6f852`;
path `results/q-01_abs.json` →
`196854f5bf555f5f96463b1d1b04fe931a66c81376dac1ce82c23891458f2396`.
Plain hashes of raw IDs, tags, or paths are forbidden because enumerable
values make them dictionary oracles. Private-gold filenames use
`case_id_hmac_sha256`.

`missing_fields` SHALL be sorted and restricted to
`question.question_date`, `gold.answer_session_ids`,
`ingest.transmitted_payloads`, `search.transmitted_query`,
`search.options.limit`, `search.options.threshold`,
`search.normalized_hit_ids`, `search.normalized_scores`,
`search.normalized_ranks`, `search.retry_attempts`, and `search.http_status`.
Unavailable answer-session IDs SHALL be represented as null and missing, never
as an invented empty list. Constants and inferred values SHALL NOT substitute
for absent persisted evidence. Imported timings SHALL be non-publishable with
reason `host_unvalidated`.

The cleanup helper SHALL accept only an absolute no-follow regular private
`guest-cleanup-plan.v1` with the same ownership/mode rule. Its closed fields
SHALL be the three literal protocol/schema/artifact identifiers; run/provider/
variant; absolute work and evidence roots; an absolute no-follow
`run_plan_path` plus its digest; and sorted targets containing the
raw private `container_tag`, its `container_tag_hmac_sha256`, sorted-unique discovery sources from
`checkpoint|guest_evidence|secure_descriptor`, and `namespace_expected`.
Targets SHALL be the deduplicated union of non-pending checkpoint tags, tags in
validated guest request/response evidence, and tags in secure descriptors,
sorted by `container_tag_hmac_sha256`. A successful Basic ingest response sets
`namespace_expected: true`.

The helper SHALL reload and revalidate that exact run plan, require its
run/provider/variant and roots to equal the cleanup plan, and derive every
descriptor path, checkout pin, expected instance, command, and environment
binding through the accepted §4.4 provider-specific expectation functions
from the run plan's provider checkout and work/evidence roots. Ambient
checkout/root/pin values and descriptor-supplied identity SHALL never replace
the plan binding.

The helper SHALL attach through the reviewed secure descriptor path, invoke
the concrete guest provider's existing `clear(containerTag)` method strictly
sequentially, and SHALL NOT launch, repair, or replace a guest during teardown.
Failure for one target SHALL be captured without preventing attempts for every
later target. Its stdout proof SHALL omit credentials, raw process identities,
raw container tags, and absolute paths.

Cleanup SHALL run in its own new session/process group so a second terminal
signal delivered to the runner's foreground group cannot terminate teardown
before proof persistence. A missing descriptor, work directory, service root,
or config path proves only that exact surface absent; it SHALL NOT be promoted
to process-group, configuration, or any other absence. Exact owned process
identity SHALL be retained before clear and process-group absence SHALL be
probed independently of directory state. Basic's public cleanup-call count
SHALL be incremented only at the observed invocation seam that actually enters
the concrete provider's final shared-service cleanup, never inferred from
candidate or target outcomes.

The cleanup proof's closed top-level fields SHALL be the three literal
protocol/schema/artifact identifiers; run/provider/variant; `trigger` from
`success|stage_failure|export_failure|SIGINT|SIGTERM`; HMAC-pseudonym-sorted unique
`targets`; `basic_public_cleanup_calls` as a nonnegative integer; sorted-unique
stable `failure_codes`; `final_absence`; and `all_absent`. Each target SHALL
contain exactly its `container_tag_hmac_sha256`, sorted-unique discovery sources,
`outcome` from `cleared|already_absent|clear_failed|absence_unproved`, a
nullable stable failure code, sorted-unique artifact references, and
`absence` with nullable booleans for `namespace`, `corpus`, `config`,
`descriptor`, `process_group`, and `work_root`. `final_absence` SHALL contain
non-null booleans for config, descriptor, process-group, and work-root absence plus
sorted-unique artifact references. For Exomem the public cleanup-call count
SHALL be zero. For Basic it SHALL be the observed count `0|1`: an
`all_absent:true` proof with any `cleared` target requires one, while an
`all_absent:true` proof whose targets are all `already_absent` requires zero.
A failed proof MAY honestly contain zero or one according to whether the real
final shared-service cleanup seam was entered; the artifact validator SHALL
reconcile that count with durable seam evidence.
`all_absent` SHALL equal, not merely claim, the conjunction of: no target has
`clear_failed|absence_unproved`; every target has `namespace: true`, every
applicable corpus/config/descriptor/process/work surface is true and every
inapplicable surface is null; all four final-absence booleans are true; and
the provider cleanup-call rule holds. For Exomem, `already_absent` is legal
only after the deterministic per-target descriptor and work root are both
proved absent. For Basic, `already_absent` instead requires per-target
namespace and corpus absence plus the aggregate final shared
descriptor/process/work/config proof; its inapplicable per-target shared
surfaces remain null.
For Basic Memory, per-target `config`, `descriptor`, `process_group`, and
`work_root` are always null because those surfaces belong to the shared
service; `final_absence` SHALL additionally contain non-null `config` absence
and proves those four shared surfaces only after every target attempt. For
Exomem, per-target descriptor, process-group, and work-root absence are
applicable booleans, while corpus/config are null; all aggregate final-absence
booleans still apply.

The repository SHALL commit deterministic Draft 2020-12 schemas generated
from strict protocol models for `memorybench-run-plan.v1`,
`memorybench-export.v1`, `memorybench-private-gold.v1`,
`guest-cleanup-plan.v1`, and `guest-cleanup.v1`. The TypeScript cleanup-plan
parser and proof emitter SHALL have conformance tests against those same
schemas. Schemas SHALL reject every structurally expressible violation using
standard Draft 2020-12 keywords: unknown/missing fields, type, literal, enum,
range, regex, unique-items, and finite closed conditional branches. They SHALL
NOT use nonstandard `$data` or claim to prove arbitrary sibling-value equality,
lexical list ordering, resolved-root containment, referenced-byte digests, or
cross-file/source reconciliation, which Draft 2020-12 cannot express.

Every accepted artifact SHALL pass three distinct gates in order: its committed
schema; the strict model including in-document cross-field validators; and the
shared artifact validator that re-reads external state. Passing a schema,
model dump, manifest status, count, or digest field alone is never evidence and
SHALL NOT authorize `VALID`.

Parity tests SHALL maintain two separate closed registries. The only permitted
schema→model acceptance differences are these in-document semantics, named per
affected field: arbitrary lexical ordering (`MemoryBenchExport.failure_codes`,
each case's `failure_codes`/`missing_fields`, private-gold `missing_fields`,
cleanup discovery/failure/evidence lists, and cleanup targets by digest);
case ordering by `MemoryBenchExport.cases.case_ordinal`;
sibling equality/inequality (`MemoryBenchRunPlan.output_root|guest_work_root|
guest_evidence_root`); keyed uniqueness across
distinct objects (`MemoryBenchExport.cases.case_ordinal`,
`GuestCleanupPlan.targets.container_tag_hmac_sha256`, and
`GuestCleanup.targets.container_tag_hmac_sha256`). `GuestCleanup.all_absent` and
Basic's public-cleanup 0/1 rule are finite conditionals over closed fields and
arrays, so they are explicitly not exceptions and SHALL be enforced with
standard `if`/`then`/`else`, `items`, `contains`, and `not` constructs.
Standard-expressible literals, branches, duplicates of whole items, and finite
fixed conditionals are not exceptions and remain schema-enforced. Any other
schema→model acceptance difference SHALL fail.

The model→artifact-validator obligation registry SHALL separately require:
no-follow file type/ownership/mode checks; resolved root containment and
disjointness; exact plan and provider-variant registry membership; checkout,
tree, and lock verification; every public HMAC pseudonym and referenced byte
digest; dataset bytes and decoded case count; exact selection membership/order
and checkpoint target agreement; cleanup-plan run-plan digest/identity/root
binding and target HMAC recomputation from the private run-plan key;
checkpoint/dataset/canonical-result reconciliation and
case completeness; cleanup discovery-union completeness; descriptor/process/
namespace/config/corpus/work-root absence; cleanup-proof/source agreement; and
public-artifact privacy scanning. These facts are external to the JSON object
and are not schema/model parity exceptions. Every obligation SHALL have an
adversarial test proving that omission or forgery is refused; any unregistered
model→artifact acceptance difference SHALL fail.

Every JSON input and recomputed JSON source in this lane — including run and
cleanup plans, dataset, checkpoint, canonical results, private gold, cleanup
proof, and persisted export — SHALL reject duplicate object member names at
every nesting depth before semantic validation. Python and TypeScript SHALL
not inherit last-member-wins behavior from their default JSON parsers.
Portable lexical absolute-path constraints SHALL be schema-enforced for every
absolute path field; only resolved containment remains a model or artifact
obligation. Sorted-unique artifact-reference lists SHALL compare and order the
whole closed reference `(root,path,path_hmac_sha256,sha256)`, not a key that
omits a field. Original source order for `answer_session_ids` is preserved and
is not a lexical-order schema exception.

Stable export failure codes SHALL be restricted to `stage_process_failed`,
`checkpoint_missing`, `checkpoint_invalid`, `checkpoint_identity_mismatch`,
`case_set_mismatch`, `phase_incomplete`, `phase_failed`, `result_missing`,
`result_duplicate`, `result_outside_root`, `result_invalid`,
`result_identity_mismatch`, `checkpoint_result_mismatch`, `hit_invalid`,
`guest_evidence_invalid`, `secure_descriptor_invalid`,
`private_gold_write_failed`, `export_write_failed`, `SIGINT`, and `SIGTERM`.
Stable cleanup failure codes SHALL be restricted to `descriptor_missing`,
`descriptor_insecure`, `descriptor_stale`, `descriptor_binding_mismatch`,
`clear_failed`, `namespace_absence_unproved`, `corpus_absence_unproved`,
`config_absence_unproved`, `process_group_absence_unproved`,
`work_root_absence_unproved`, and `cleanup_proof_write_failed`.

Source reads SHALL use no-follow regular-file checks and remain inside the
resolved pinned run root. Canonical `results/<question-id>.json` discovery
SHALL be independent of untrusted checkpoint `resultFile` strings. Exactly one
canonical result SHALL exist for every expected completed search and no extra
result SHALL exist. Dataset, checkpoint, and canonical result SHALL agree on
question ID, question, type, and ground truth. Checkpoint and canonical result
SHALL agree on container tag and result-array length. Checkpoint inline
`results`, when present, SHALL have ordered semantic deep equality with the
canonical `results`, preserving numeric values; parsed JSON values are never
misrepresented as source-byte equality. Missing, duplicate,
outside-root, non-finite, or disagreeing evidence SHALL produce a partial
export with the applicable stable failure code and terminal `INVALID`; no
source wins by precedence.

Target discovery SHALL complete and retain its validated union before public
projection so a later dataset/result/export failure cannot erase a namespace
already known from checkpoint, guest evidence, or secure descriptors. Source
files within checkpoint, guest-evidence, and descriptor inputs SHALL be
validated independently: one malformed candidate contributes none of its own
untrusted identity, SHALL NOT erase valid sibling/source targets, and adds
`checkpoint_invalid`, `guest_evidence_invalid`, or
`secure_descriptor_invalid` respectively to the partial export. Thus cleanup
still attempts every positively validated target while the malformed discovery
surface makes the overall run `INVALID`.
Source
agreement and result uniqueness SHALL be established before any hit list is
selected; disagreement or conflicting duplicate canonical results emits no
selected hits and no source wins. Before persistence, the runner SHALL apply
the shared public privacy scanner to every decoded JSON string, including
object keys. For the Exomem guest's recognized stringified session capture,
it SHALL additionally decode the complete embedded message array and scan
its keys and values plus all surrounding capture text. Invalid or incomplete
embedded JSON SHALL refuse export. JSON escaping SHALL neither fabricate a
local-path finding from ordinary message text nor hide an actual private path.
Opaque runtime identifiers and gold-field checks SHALL remain enforced.
Exact question text and complete capture/v2 source bodies MAY retain public
filesystem examples only when case-local provenance matches the repository's
canonical source identity, actual dataset bytes and exact frozen 25-case
selection. A caller-declared digest alone SHALL NOT authorize an exception.
The exception SHALL apply only to the exact question or hit-content field;
changed, truncated, concatenated or cross-case bodies SHALL remain scanned.
Private runtime identifiers, the HMAC key and all plan runtime roots SHALL
always refuse export, including when JSON escaped or present in public source.
The runner SHALL then
re-read the persisted artifact and invoke the full shared artifact validator
before terminal `VALID`; a model dump or in-memory projection is insufficient.

#### Scenario: Serialized session newlines are not private drive paths
- **WHEN** a guest message ends a possessive word with a colon and newline,
  and the capture embeds that message as JSON
- **THEN** export scans the decoded message and permits the ordinary text
- **AND** actual private drive, UNC and home paths in capture prefixes,
  message values or message keys still refuse persistence

#### Scenario: Public source examples do not authorize runtime leakage
- **WHEN** an exact canonical case source body contains a public path example
- **THEN** export preserves the body after verifying its frozen source provenance
- **AND** the same body in another case or field, altered content, and private
  runtime values anywhere still refuse export

The runner SHALL write a started manifest before provider work, export complete
or partial evidence before cleanup, invoke cleanup from one `finally` path on
success, failure, or handled interruption, persist the cleanup proof, and
finalize the manifest last. Only a complete export with `all_absent: true`
SHALL be `VALID`. Provider-stage, export, interruption, cleanup, or absence-
proof failure SHALL be `INVALID`; `BLOCKED` SHALL be limited to prerequisites
that fail before provider action. Status SHALL be computed independently from
the process exit code. Exit precedence SHALL be: caught SIGINT/SIGTERM returns
130/143 while retaining manifest `INVALID` and any cleanup failure; otherwise
unproved cleanup or `all_absent != true` returns 3; otherwise pre-provider
`BLOCKED` returns 2; otherwise `VALID` returns 0; every remaining `INVALID`
returns 1. If export, cleanup-proof, or final-manifest persistence fails, the
manifest SHALL remain nonterminal `started` rather than fabricate completion.
Export or final-manifest persistence failure computes in-memory `INVALID` and
returns 1 when no higher-precedence condition applies, even though the durable
manifest remains `started`. Cleanup-proof persistence failure is externally
unproved cleanup and returns 3 unless a caught signal has precedence.

#### Scenario: Export cannot invent missing evidence
- **WHEN** the pinned MemoryBench artifacts omit transmitted queries, options,
  normalized hit identifiers, or answer-session IDs
- **THEN** the export records the corresponding closed missing-field entries
  and never substitutes source constants, inference, or an empty list

#### Scenario: Ground truth remains private
- **WHEN** a canonical MemoryBench result contains a question and ground truth
- **THEN** the public export contains only the public case projection and a
  protected private-gold reference, not the raw result envelope or answer

#### Scenario: Cleanup cannot launch during teardown
- **WHEN** a descriptor is absent, stale, insecure, or contradictory
- **THEN** cleanup refuses or records unproved absence without starting,
  repairing, or replacing a guest service

#### Scenario: Manifest finalizes after cleanup proof
- **WHEN** provider stages finish, fail, or receive a handled termination signal
- **THEN** the runner exports available evidence, performs cleanup, persists its
  proof, and only then finalizes validity from export completeness and
  `all_absent`

### Requirement: Canonical LongMemEval-S Selection Is Reproducible And Bound
The 25-case comparative cohort SHALL be the closed `lme-selection.v1` artifact
at `benchmarks/equivalence/subsets/lme-s-25.json`. It SHALL bind repository
`xiaowu0162/longmemeval-cleaned`, revision
`98d7416c24c778c2fee6e6f3006e7a073259d48f`, filename
`longmemeval_s_cleaned.json`, SHA-256
`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`,
277383467 bytes, 500 rows, and canonical census knowledge-update 78,
multi-session 133, single-session-assistant 56, single-session-preference 30,
single-session-user 70, temporal-reasoning 133. The exact canonical type
counts overlap the independently required abstention census of 30 and total
500. The exact canonical type
order is `single-session-user`, `single-session-assistant`,
`single-session-preference`, `multi-session`, `temporal-reasoning`,
`knowledge-update`. A row is abstention iff its ID ends `_abs`, but its type
is still required and validated. The algorithm hashes UTF-8
`question_id + dataset_sha256` without a delimiter, orders by
`(digest_hex, question_id)`, selects three non-abstentions per canonical type
then seven abstentions, and refuses blanks, duplicate IDs, unknown types,
bad source facts, undersized strata, or a non-identical regeneration.

Direct canonical mode and a frozen MemoryBench explicit tier SHALL load only
the repository-owned artifact, revalidate source facts and regenerated ordered
membership before reader/provider construction, and persist
`selection_artifact_path`, `selection_artifact_sha256`, and
`selection_algorithm_version` in started and terminal evidence. A generic
`--pilot 25` SHALL be refused as a canonical/comparative substitute.

#### Scenario: Altered plan IDs cannot select a same-cardinality cohort
- **WHEN** a canonical MemoryBench plan omits, reorders, or replaces one of
  the 25 committed IDs
- **THEN** preflight is BLOCKED before any stage or provider construction

#### Scenario: Direct canonical mode cannot use a substituted artifact
- **WHEN** the repository artifact or its source identity differs from exact
  regeneration of the verified dataset
- **THEN** the direct runner refuses before reader/provider construction

### Requirement: Registration Overlay Is Exact And Reversible
Materializing guest providers into the detached pinned MemoryBench checkout
SHALL add only the eight lockfile-listed §4.4 provider/test files plus the
single §4.5 entrypoint
`src/cli/commands/competitive-ingest.ts`, and modify only the three registered
integration files. The entrypoint is invoked directly and does not add a fourth
integration edit. Verification SHALL recompute every additive hash,
pre/postimage, checkout pin, index/worktree state, and canonical binary
registration diff. Restore SHALL remove only byte-identical additive files,
reverse that exact diff, and prove the original pristine tree; any extra or
locally modified path SHALL refuse materialization, verification, or restore.

#### Scenario: Registration drift refuses verification
- **WHEN** a registration postimage, canonical diff byte, additive provider
  file, or unrelated checkout path differs from the lock
- **THEN** setup refuses the state and does not repair, delete, or overwrite it

### Requirement: Provider-Visible Identities Remain Neutral
When an upstream harness exposes evidence-labelled identifiers to a guest
transport, the transport SHALL preserve the public harness identity for
protocol correlation but derive a neutral positional or digest identity
before any product renderer or index sees it. The private mapping MAY appear
only in protected evidence and SHALL never enter provider-visible bytes.

#### Scenario: Abstention marker cannot enter rendered memory
- **WHEN** a MemoryBench session ID contains the `_abs` marker
- **THEN** the wrapped renderer receives a neutral derived document ID and
  neither the raw ID nor the marker occurs in rendered content
