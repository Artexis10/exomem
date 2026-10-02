## ADDED Requirements

### Requirement: Cloud service promptly consumes durable semantic debt

Under `service-v1`, the owning service SHALL promptly wake to consume durable deferred semantic work after canonical writer lease release without depending on a full reconcile scan or unrelated graph/full-refresh work. Notifications SHALL be coalesced hints; durable receipt state SHALL remain authoritative across missed notifications, failure, shutdown and restart. Existing successful inline indexing SHALL remain available. Exact-source generation and publication fences SHALL be preserved: partial or superseded work MUST NOT clear newer debt or be advertised as current. Poisoned work SHALL be retained and retried with bounded backoff without preventing other eligible work from progressing.

#### Scenario: Semantic receipt arrives between reconcile cycles
- **WHEN** a service-profile canonical write records durable semantic debt and releases its writer lease
- **THEN** its owning service attempts bounded semantic recovery promptly without waiting for the next full reconcile interval
- **AND** completion retires only the matching published generation

#### Scenario: Newer edit supersedes in-flight recovery
- **WHEN** a source changes while older deferred semantic work is being prepared
- **THEN** older preparation is not advertised as current and cannot clear the newer receipt
- **AND** current-source work remains eligible for recovery

#### Scenario: Lost hint and poisoned work
- **WHEN** a wake hint is lost or a receipt repeatedly fails
- **THEN** restart and periodic recovery still discover durable outstanding work
- **AND** bounded backoff for the failing receipt does not strand other eligible receipts

### Requirement: Cloud interactive work progresses during bulk indexing

The service profile SHALL bound bulk/recovery model work at actual encode-batch boundaries and preserve foreground model admission, query participation and the existing request-worker reserve. Bulk jobs MUST NOT hold all foreground admission for an entire arbitrarily large input. Recovery SHALL allow a new eligible small parent to reach model admission while an older large parent is being prepared; limiting turns only between complete parents does not satisfy this requirement. There SHALL be at most one small-recovery preparation and one bulk-recovery preparation concurrently, with aggregate prepared data bounded to 64 MiB. A bulk source over 16 MiB or predicted preparation exceeding the budget SHALL remain durable with a visible budget refusal, without constructing the oversized representation or blocking healthy work.

Outer request admission SHALL remain nonblocking. Fair execution waiters SHALL already be admitted and count against the same reserved model budget, including service-owned callers. Foreground precedence SHALL include a bounded aging turn for bulk work so sustained interactive traffic does not permanently starve recovery. Partial parent preparation MUST NOT relax exact-parent publication, source-version or vector-identity checks. Local default admission behavior SHALL remain unchanged. Occupied bulk skips MUST NOT consume reserved small execution attempts. Service ONNX dynamic-int8 SHALL release execution between its actual serial inference calls while preserving admitted-work custody and other backend batch policies.

Existing derived-component custody SHALL remain with its current owner when semantic execution is delegated. Queued or publishing work SHALL remain pending rather than being completed or recorded as a failed attempt. Exact semantic publication and the component's own canonical/custody completion fence SHALL both be satisfied before semantic completion. Repeated pending observations MUST NOT supersede in-flight semantic receipts by minting new revisions. Eviction from a bounded volatile proof cache MUST NOT prevent a larger canonical batch from completing: cold proof SHALL validate guarded current source, exact chunk/unit coverage and encoder identity from one stored snapshot, or leave work pending for bounded replay.

The aggregate preparation budget SHALL reserve capacity for small work and include expanded text, both vector projections at their actual width and peak intermediate/publication copies. Optional reuse of old vectors SHALL be size-checked before reading all old rows and skipped when it would exceed the allowance. Classification MUST NOT invoke model inference merely to determine work size. Bounded small-source preparation SHALL charge its actual no-encode chunk/unit shape before vector allocation, rather than treating every source line as both projections.

#### Scenario: Large parent and a small live update compete
- **WHEN** a large parent is being indexed and a bounded ordinary update arrives
- **THEN** the live update can obtain model execution without waiting for the entire large parent
- **AND** the existing parent is published only after its exact complete preparation is validated

#### Scenario: Sustained foreground work
- **WHEN** eligible foreground work continues to arrive during a bulk/recovery operation
- **THEN** bounded aging gives bulk/recovery work execution turns without consuming the reserved non-model request workers
- **AND** both workloads retain durable progress and truthful refusal/degradation reporting

#### Scenario: Excess model requests
- **WHEN** simultaneous model requests exceed admitted capacity
- **THEN** excess requests refuse promptly rather than waiting outside the admitted budget
- **AND** status and lexical requests retain their reserved request-worker capacity

#### Scenario: Oversized recovery parent
- **WHEN** a bulk parent exceeds the source or prepared-data budget
- **THEN** its receipt remains durable with an observable resource-budget refusal
- **AND** eligible small parents continue to progress without allocating the oversized representation

#### Scenario: A large old version becomes a small replacement
- **WHEN** a small current parent has an old stored representation exceeding the reuse allowance
- **THEN** recovery re-encodes the bounded replacement instead of allocating all old vectors
- **AND** the current parent is published only after its normal exact-source checks

#### Scenario: Delegated derived component awaits semantic publication
- **WHEN** the existing component owner has submitted semantic work that is still queued or encoding
- **THEN** its semantic component remains pending without failure backoff or duplicate revision minting
- **AND** only exact semantic publication plus current component custody can complete it
