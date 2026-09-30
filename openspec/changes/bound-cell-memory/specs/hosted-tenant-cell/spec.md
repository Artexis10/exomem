## ADDED Requirements

### Requirement: Cell Resident Memory Is Bounded Independently Of Vault Size

A hosted cell's resident memory SHALL NOT grow linearly with the size of its vault's Markdown corpus or chunk count. Once a cell is idle and its reaper has released idle RAM caches, its resident set SHALL consist of the fixed runtime (interpreter, libraries, and the embedding model when loaded) plus bounded caches. It MUST NOT include a whole-corpus Python projection that the vault's derived sidecars already hold. Freed memory SHALL be returned to the operating system after model reaps and derived-index drain batches. A profiling harness SHALL measure resident memory at fixed points, and every change to a cell's memory behaviour MUST be judged against it. Tenant isolation is unchanged: no memory optimisation may place one tenant's plaintext in a process that serves another.

#### Scenario: Idle memory does not scale with the vault

- **WHEN** the profiling harness runs a cell to idle over a small vault and over a large vault at least ten times its Markdown size
- **THEN** the idle resident memory of the large-vault cell exceeds the small-vault cell's by no more than the configured bounded-cache budgets

#### Scenario: Vectors are not held resident in a hosted cell

- **WHEN** a hosted cell serves vector search
- **THEN** it uses the vec0 backend and never loads the full embedding matrix into Python memory
- **AND** a cell whose image cannot load the vector extension refuses to start with a stable error code rather than falling back to the in-memory matrix

#### Scenario: Freed memory is returned after a reap

- **WHEN** the model reaper releases the model or idle RAM caches in a hosted cell
- **THEN** the cell asks the allocator to return freed memory to the operating system
- **AND** the harness shows resident memory falling to within a stated margin of the post-import floor plus the bounded caches

#### Scenario: The first index build stays under the cell limit

- **WHEN** the harness runs a cell's first index build over the large synthetic vault
- **THEN** the peak resident memory stays below the cell's configured memory limit with the stated headroom
