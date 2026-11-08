## MODIFIED Requirements

### Requirement: Replication-mechanism independence
Writer coordination SHALL NOT depend on Syncthing or any other specific vault replication product. The operator SHALL remain responsible for providing shared storage or an external replication mechanism appropriate to the deployment. In file mode, every replica receives the same canonical vault through that mechanism. A vault in store mode (`make-vault-store-canonical`) keeps its pages in the collection store, which file replication does not carry. Multi-host coordination SHALL therefore NOT serve or activate a store-mode vault until a takeover path carries the store itself.

#### Scenario: Self-hoster uses a different replication layer
- **WHEN** replicas receive the same canonical vault through shared storage, Unison, rsync, Git-based automation, or another external mechanism
- **THEN** Exomem applies the same writer-lease behavior without requiring Syncthing APIs or configuration

#### Scenario: Store mode refuses multi-host coordination
- **GIVEN** a vault is in store mode or is about to enter it
- **WHEN** the configured writer-lease coordinator uses the multi-host topology, which is the default when `EXOMEM_WRITER_LEASE_TOPOLOGY` is unset
- **THEN** the service refuses to start the vault and the activation command refuses, each with `VAULT_STORE_MULTI_HOST` naming `EXOMEM_WRITER_LEASE_TOPOLOGY=single-host`
- **AND** no vault byte changes

#### Scenario: Store mode refuses an install with no coordinator
- **GIVEN** a vault is in store mode or is about to enter it
- **WHEN** no writer-lease coordinator is configured in the process environment
- **THEN** the service refuses to start the vault and the activation command refuses, each with `COLLECTION_STORE_COORDINATION_REQUIRED`
