## ADDED Requirements

### Requirement: Instruments answer closed questions from a repository registry

Every model-backed measurement SHALL be an instrument answering one entry of a repository question registry. An entry SHALL fix the question type, a versioned template, a versioned label map over a closed label set plus `abstain`, a versioned unit scope and an admitting fixture set. No environment value, runtime configuration or vault content SHALL add, select or alter an entry.

The `pair.relation` entry SHALL use template `nli-pair-v1`, which is the two unit texts as a classification pair in both orders, and label map `relation-v1`. Its closed labels are `contradicts`, `refines`, `restates`, `neutral` and `abstain`, and the map SHALL apply these rules in order:

1. `contradicts` when the minimum directional contradiction probability is at least 0.93;
2. `restates` when the minimum directional entailment probability is at least 0.95;
3. `abstain` with reason `directional_asymmetry` when exactly one direction's contradiction probability is at least 0.93;
4. `refines`, naming the refining side, when the maximum directional entailment probability is at least 0.95;
5. `neutral` otherwise.

An instrument SHALL abstain with `input_too_long` rather than truncate an input.

An instrument's output SHALL be probabilities over the closed set, never sampled or generated text. A template that places vault text SHALL place it only inside delimited data slots, and SHALL NOT place it in instruction position.

#### Scenario: A one-way contradiction is its own state

- **WHEN** one direction's contradiction probability is 0.97 and the other's is 0.40
- **THEN** the verdict is `abstain` with reason `directional_asymmetry`
- **AND** it is neither `contradicts` nor `neutral`

#### Scenario: Refinement names its direction

- **WHEN** text B entails text A at 0.98 and A entails B at 0.30
- **THEN** the verdict is `refines`, and B is named as the refining side

#### Scenario: Runtime configuration cannot add a question

- **WHEN** an environment value or vault file names a question type absent from the registry
- **THEN** no instrument senses under that name

### Requirement: Readings are append-only records in a durable per-vault ledger

Each instrument judgement SHALL be recorded as a reading in an append-only ledger at `<vault state dir>/sensing/readings.sqlite`, resolved through the single state-root seam. The ledger SHALL be outside the vault and separate from the disposable dreamer sidecar. It SHALL refuse every update and delete of a reading or an instrument record.

A reading SHALL record:

- its instrument identity: model, revision, weights sha256 or `unpinned_weights`, runtime and runtime version, template version, label-map version, fixture-set version and placement;
- each input's unit ref, page path, the sha256 of the exact text fed in, and the extractor version;
- the full probability vector for each direction;
- the verdict, including abstain and its reason;
- `sensed_at`;
- the placement.

The reading id SHALL be the hash of the question type, the instrument id and the ordered input text hashes. The instrument id SHALL cover the fields that determine the vectors (model, revision, weights, runtime, runtime version, template version) and SHALL exclude the label-map and fixture-set versions.

The ledger SHALL NOT store vault text. Wiping the dreamer sidecar SHALL NOT touch it. The ledger SHALL be named in the state-root backup guidance, and SHALL be a portable-derived member of hosted export and restore once cells sense.

#### Scenario: The ledger refuses rewriting history

- **WHEN** any process attempts to update or delete a stored reading
- **THEN** the statement is aborted and the reading is unchanged

#### Scenario: Re-sensing the same inputs appends nothing

- **WHEN** the same instrument senses the same ordered input texts again
- **THEN** the reading id is the same and no second row is appended

#### Scenario: A sidecar wipe keeps the evidence

- **WHEN** the dreamer sidecar is wiped and reseeded
- **THEN** every reading remains in the ledger and is consumed again without re-sensing

### Requirement: Readings invalidate by input content and migrate lazily across instruments

A reading SHALL be consumed only while the current text hash of each input unit equals the hash the reading recorded. An edited input's reading SHALL become `stale`: it is kept, not consumed, and its pair is queued again. An edit to other units or to prose outside every in-scope unit SHALL re-sense nothing.

A label-map change SHALL re-derive verdicts from the stored vectors without sensing.

When the active instrument changes (a new pin, a new runtime version, or a retired API model), the earlier readings SHALL be kept and SHALL NOT be consumed. The affected pages SHALL report `evidence_complete: false` until a lazy re-sense drains. That re-sense SHALL order pairs that previously carried a consumed edge first.

#### Scenario: An unrelated edit re-senses nothing

- **WHEN** a page's prose changes outside its in-scope units
- **THEN** its pairs keep their current readings and no pair is queued

#### Scenario: An edited unit goes stale

- **WHEN** an input unit's text changes
- **THEN** the pair's old reading is kept, is not served, and the pair is queued for the active instrument

#### Scenario: A new pin migrates without discarding

- **WHEN** the active instrument id changes
- **THEN** earlier readings stay in the ledger, are not served, the page reports `evidence_complete: false`, and previously served pairs are re-sensed first

### Requirement: Inference runs only in a supervised disposable sensor worker

Model inference SHALL run only in a disposable child process supervised from the dreamer's loop. The dreamer thread and request threads SHALL NOT load or run a model.

- **Launch.** The supervisor SHALL launch a child only when all of these hold: sensing is `on` (`EXOMEM_SENSING`, else the per-machine config key `sensing`, default `off`, effective only while the dreamer runs); the dreamer's own gate would run a tick; the sense queue is not empty; and the worker's hourly budgets have room.
- **Termination.** It SHALL terminate the child when any gate closes, and SHALL kill it in quiet mode, under auto-quiet pressure and in standby, so its memory returns to the host.
- **Budgets and priority.** The worker SHALL spend at most 300 CPU-seconds and 600 judgements per rolling hour, including model load. It SHALL run at the lowest OS priority on CPU until co-tenant GPU pressure detection ships, and SHALL exit after 60 seconds without work.
- **Isolation.** The child SHALL read only the sense queue and the ledger, SHALL append only to the ledger, and SHALL NOT read or write the vault, take the writer lease, or schedule index work.
- **Refusal.** A child that cannot admit its instrument SHALL exit with a named refusal. The supervisor SHALL NOT relaunch it until the setting or pin changes.

#### Scenario: Quiet mode returns the memory

- **WHEN** a sensor child is running and the compute mode becomes `quiet`
- **THEN** the child process is terminated within one supervisor poll and is not relaunched while quiet holds

#### Scenario: A foreground request closes the gate

- **WHEN** a foreground request arrives while the child senses
- **THEN** the supervisor terminates the child and relaunches only after the dreamer's idle gate reopens

#### Scenario: The budget holds across launches

- **WHEN** children have spent the hourly CPU or judgement budget
- **THEN** no child is launched until the rolling window frees budget

#### Scenario: Sensing off is invisible

- **WHEN** sensing is off, or the instrument is refused
- **THEN** no ledger is created, no child starts, and every product surface is byte-identical to a build without sensing, apart from the diagnostic status

### Requirement: Pair proposers read stored data with per-pair monotone predicates

The dreamer SHALL propose pairs for sensing from stored data only, and SHALL NOT encode text. A proposal predicate SHALL depend only on the two units and their own pages. The predicates are:

- a cosine of at least a fixed threshold between stored unit vectors of the ranked encoder, whose source text hash equals the unit's current hash;
- a graph edge between the two pages;
- a shared authored link target between two pages with different knowledge dates.

Selection SHALL NOT be top-k or corpus-relative. Only pairs across two pages SHALL be proposed, and identical texts SHALL NOT be paired. A page SHALL propose at most 128 pairs in a fixed deterministic order. A page whose candidates exceed that cap SHALL be marked capped.

#### Scenario: A third page cannot create or remove a pair

- **WHEN** a page unrelated to two units is added, edited or withheld
- **THEN** whether the two units are proposed is unchanged

#### Scenario: The threshold is per pair

- **WHEN** many units exceed the cosine threshold against one unit
- **THEN** every such pair is proposed up to the page cap, and none is dropped because of the others' scores

### Requirement: Epistemic projections are deterministic and keep uncertainty

The dreamer SHALL project readings into typed unit edges, contradiction components and refinement and supersession chains in time order. Each projection SHALL be a pure function of the ledger, the graph and the pages. Projections SHALL NOT depend on append order or `sensed_at`.

Edge fingerprints SHALL bind to the question type, the ordered input text hashes, the verdict and the direction, and SHALL NOT bind to the reading id.

Directional asymmetry, abstention and disagreement between active instruments SHALL each be kept as a distinct state. A disagreement SHALL carry every verdict and SHALL NOT be collapsed into one. Every served sensed item SHALL name its verdict, probability, instrument and the instrument's fixture precision for that label.

#### Scenario: Replay is byte-identical

- **WHEN** the ledger is replayed into a fresh dreamer sidecar over the same pages and graph
- **THEN** the projected edges, their fingerprints and every served status are byte-identical to the original

#### Scenario: Instruments that disagree stay disagreeing

- **WHEN** two active instruments read the same inputs as `contradicts` and `neutral`
- **THEN** the edge state is `instruments_disagree`, both verdicts are served, and neither is counted as an open contradiction

### Requirement: Sensed findings are delivered pull-first at the point of use

When a page is read, or is resolved as an activation anchor, the response SHALL carry an `epistemic_status` for that page when at least one of its counts is non-zero. The status SHALL contain:

- a status line;
- `refined_by_later`: the distinct released pages with a later knowledge date whose unit refines one of this page's units;
- `open_contradictions`: the distinct released pages with a current contradiction edge, where neither page is superseded or archived and no authored supersession joins them;
- the released items behind each count;
- the bounded refinement chain;
- the contradiction component size;
- `evidence_complete`.

The status SHALL NOT rank, reorder, filter or gate anything, and SHALL count released pages only. On activation it SHALL be attached outside the packet cache and charged to the packet budget.

Tensions on active or recent work SHALL reach the agent only through the existing upkeep block, under its caps and delivery rules, as sensed families. Sensed families SHALL NOT suppress, reorder or alter structural families. A reading SHALL enter canon only through a write the agent authors with an existing governed writer, citing the reading id. No new push channel SHALL exist.

#### Scenario: A read shows what later notes did

- **WHEN** a page is read, two later released pages refine its units, and one released page contradicts it with no supersession between them
- **THEN** the read carries `epistemic_status` with the line `refined by 2 later notes; 1 open contradiction`
- **AND** the body, ordering and every other field of the read are unchanged

#### Scenario: Nothing sensed means nothing added

- **WHEN** a page has no current edge with a non-zero count
- **THEN** its read and activation responses carry no `epistemic_status` field

### Requirement: Sensed items are released per caller

A reading SHALL be consumable for a caller only when every input unit's page is released to that caller. Items, counts, chains and components SHALL be recomputed per request from released edges. Under a governed policy, a withheld page SHALL be indistinguishable from an absent one in every sensed field, count, chain and component a restricted caller receives.

The sensed items of a capped page, in its own status and in the counts of pages paired with it, SHALL be served only to owner-bound principals. The time at which a pair is sensed MAY depend on the whole queue; what is served SHALL NOT.

#### Scenario: A withheld contradicting page is absent

- **WHEN** a restricted caller reads a page whose only contradiction is with a withheld page
- **THEN** the response is byte-identical to the same read in a vault where the withheld page does not exist

#### Scenario: A withheld page breaks a component

- **WHEN** a withheld page links two released pages in a contradiction component
- **THEN** a restricted caller sees the two released pages in separate components, exactly as in the absent twin

#### Scenario: A capped page is owner-only

- **WHEN** a page's proposal cap binds
- **THEN** its sensed items are served to the owner and absent for every restricted caller

### Requirement: Instrument placement is local by default and opt-in elsewhere

Personal vaults SHALL sense with local instruments by default. An API instrument SHALL be an opt-in placement per vault, enabled by the owner for personal vaults and by an explicit per-tenant grant for Cloud (off by default). Its identity SHALL be provider, model id and version or snapshot date, flagged `unpinned_weights`. It SHALL obey the same template, closed-label, abstain and fixture-admission contract. Its request SHALL carry only delimited slot texts. A vendor's retirement of the model SHALL trigger the ordinary instrument migration.

Cloud cells SHALL run the dreamer, and SHALL sense through an in-cluster shared plane. No third-party API SHALL receive vault text unless the tenant opted in. The Cloud plane SHALL be measured against the acceptance measures in `docs/hosted-inference-boundary.md`.

#### Scenario: No third-party API by default

- **WHEN** a Cloud cell senses without a tenant API grant
- **THEN** no request containing vault text leaves the cluster

#### Scenario: An API instrument is identified honestly

- **WHEN** an opted-in vault senses through an API model
- **THEN** each reading's instrument identity names the provider, model id and snapshot date with `unpinned_weights: true`

### Requirement: Instrument admission is verified at the exact pin with negative twins

Each question's fixture set SHALL contain English, same-language non-English and mixed-language pairs over every label. Each label SHALL have a negative twin: a minimally different pair that must not receive that label. The exact pinned bytes SHALL pass every fixture through the production instrument and label map, and one miss SHALL refuse the instrument. The real pin SHALL be exercised only in the dedicated model lane. The lean suite SHALL use stub instruments. Replay reproducibility, egress twins and the worker's resource probes SHALL be tested.

#### Scenario: A fixture miss refuses the instrument

- **WHEN** the pinned model gives any fixture pair the wrong label
- **THEN** the instrument is refused and no reading is appended under it
