# Substrate companion control-plane contract

This document is the repository boundary for the managed Exomem service. It
defines what the companion Substrate change must provide around private,
single-vault Exomem cells. It is intentionally provider-neutral at the cell
boundary even though the first billing adapter is Paddle.

## Ownership split

| Concern | Exomem | Substrate / deployment |
| --- | --- | --- |
| Canonical knowledge | Markdown, original media, governed history, schema, portable review state | never copied into shared account or billing rows |
| Tenant isolation | fail-closed single-vault cell config, private auth, mutation safety, readiness | process/container scheduling, private network, isolated mounts and secrets |
| Public identity | no consumer identity provider or public session | invites, accounts, sessions, consumer OAuth and recovery |
| Routing | verify trusted cell-bound context; expose no path selector | immutable account-to-cell registry and public gateway |
| Commands | versioned registry-derived schemas, results, errors, retry semantics | forward the compatible contract without reimplementing command behavior |
| Entitlements | enforce provider-neutral capability/resource policy | Paddle adapter, internal entitlement projection, suspension and quota decisions |
| Product UI | private cell data/Review interfaces only | Exomem Home, friendly capture, Connections, Review and Account UI |
| Portability | quiesce, canonical archive/manifest verification, staged restore, cell checkpoint | backup/export delivery, object storage, retention and lifecycle orchestration |
| Encryption | reject unsafe cell config; keep content out of operational logs | volume/backup encryption, KMS, secret injection and access controls |
| Deletion | seal and report cell-local quiesced facts | revoke access, destroy storage/backups/keys, remove mappings and update account/billing |

Neither repository should grow half of the other's responsibility. In
particular, Exomem cells do not query account tables or Paddle, and Substrate
does not parse or mutate tenant Markdown directly.

## Account-to-cell registry

An authenticated public principal resolves to one account subject. Each active
hosted account has exactly one active cell mapping. Missing or ambiguous mappings
fail closed; there is no default, last-used, or neighboring fallback.

The internal mapping needs, at minimum:

- immutable account subject and opaque tenant/cell/vault IDs;
- private cell endpoint plus a reference to that cell's unique credential;
- lifecycle and suspension state;
- pinned Exomem release and gateway/cell protocol compatibility;
- current internal entitlement revision and resource policy; and
- timestamps/revisions needed for atomic compare-and-set lifecycle changes.

Email, display name, public slug, Paddle IDs, vault paths, raw credentials, and
tenant content do not become cell identifiers. Public APIs do not expose the
private endpoint, cell credential, or a caller-controlled tenant/cell selector.
Registry updates are privileged, audited, and atomic with routing state.

## Public gateway contract

For every command or transfer, Substrate:

1. authenticates the public session and derives the immutable account subject;
2. resolves exactly one active account-to-cell mapping;
3. checks suspension, internal entitlement, quota, and protocol compatibility;
4. selects the mapped private endpoint and credential;
5. forwards a known registry command, validated arguments, request ID, protocol
   version, principal/cell-bound retry context, and provider-neutral limits; and
6. preserves the cell's structured result or stable error without weakening its
   governance and filesystem checks.

Values that look like tenant IDs, cell IDs, internal hosts, or vault paths in a
public body, URL, query, cookie, or untrusted header never affect routing. A
conflicting selector is rejected before contacting a cell. The gateway sends its
private service identity to the cell, not the consumer's raw OAuth bearer token.

Public upload/download grants are short-lived, audience- and operation-scoped,
and bound to the authenticated subject and mapped cell. Paths or multipart fields
cannot override that binding. A grant never reveals or acts as a cell master
credential. Idempotency namespaces include principal, cell, command, and
canonical request digest so equal public keys cannot collide across accounts.

## Internal entitlements and Paddle adapter

Substrate owns Paddle products/prices, checkout, webhooks, customer portal,
subscription reconciliation, refunds/cancellations, sandbox/live separation, and
complimentary alpha access. Paddle events are inputs to an internal entitlement
model, not the runtime authorization format. Webhook processing and
reconciliation must be idempotent and tolerate retry/out-of-order delivery.

The gateway authorizes from the current internal entitlement revision before
forwarding. Provisioning/startup projects only bounded provider-neutral policy to
the cell—for example retrieval mode, storage/upload limits, and optional worker
grants. The cell enforces that policy as defense in depth. Public command flags
cannot raise a tier.

No Paddle customer, price, transaction, or subscription identifier—and no Paddle
credential—crosses the gateway/cell contract. A cell never calls Paddle during
startup, readiness, command execution, transfer, export, or deletion sealing.
Paddle MCP is allowed as an operator/development tool for the Substrate billing
lane; it is not production infrastructure or an Exomem dependency.

## Exomem Home and product surfaces

Substrate owns the invite-to-value journey: invite acceptance, session creation,
first-run setup, Exomem Home, friendly capture, Connections, Review integration,
and Account/billing UI. These surfaces use the same authenticated gateway and
registry-derived command semantics as MCP; they do not gain direct filesystem or
cell-database access.

Home may retain account/product state and content-free operational progress. It
must not turn canonical notes, evidence, extracted text, query history, or search
results into a second shared source of truth. User-visible capture/review writes
commit through the mapped cell's normal mutation boundary.

## Provisioning, release, and recovery

Substrate provisioning creates an opaque identity, tenant key/secret references,
isolated vault/state/log mounts, unique cell credentials, resource policy, and a
pinned release. It invokes Exomem's idempotent initializer only against an empty
staged root, verifies private readiness, and publishes the account mapping only
after the cell is safely serviceable. A retry adopts the same cell; it does not
create another vault or overwrite incompatible data.

The gateway declares a protocol version. Additive optional fields may roll out
within a compatible contract; removing/changing commands, parameters, stable
errors, transfer rules, or retry semantics requires a coordinated gateway/cell
version and conformance fixtures. Roll out by pinned cohort/canary, not by
silently mixing incompatible releases.

Rollback stops new routing, drains or stops the affected cell, and starts the
previous protocol-compatible image against the same canonical vault. Canonical
data is not rewritten to make rollback pass. Derived stores may be deleted and
rebuilt. Before a canonical format migration, Substrate obtains and verifies a
snapshot; an incompatible downgrade restores that snapshot into a new staged
root rather than overlaying the live vault.

## Backup and user-export delivery

User exports and provider backups use Exomem's same canonical archive/manifest
contract. Substrate orchestrates quiescence, receives an opaque private artifact
reference plus size/digest metadata, and keeps the artifact private until a
separate authorized delivery or storage step.

Substrate/deployment owns:

- tenant-key encryption, encrypted object storage and KMS integration;
- retention, backup cadence, regional/storage policy and restore selection;
- authenticated, expiring user-download delivery; and
- deletion of stored artifacts under the applicable retention/deletion policy.

Exomem owns manifest classification and verification, but does not create public
download URLs or claim provider backup durability. Restore is prepared into a
new root, validated completely, and atomically published only after the target
cell is unrouted and quiesced. Derived indexes rebuild afterward.

## Suspension and destructive deletion

Deletion is an orchestrated control-plane workflow, not a model-facing Exomem
command. The order is:

1. atomically suspend the account and stop all public routing;
2. revoke sessions and outstanding transfer/export delivery authority;
3. quiesce the cell and drain mutations/background writers;
4. optionally obtain the policy-required final verified snapshot;
5. ask Exomem to seal the cell and persist its idempotent deletion checkpoint;
6. stop the process and destroy live storage, backup objects, tenant keys and
   cell credentials according to policy;
7. remove the active registry binding and update account/Paddle state; and
8. record a content-free completion audit event.

Retries resume from durable checkpoints. Substrate must not report deletion
complete while routing, storage, backups, keys, or required billing/account work
remain. Exomem reports only facts it can prove locally—routing was declared
stopped, writers drained, and the cell sealed. It does not cancel billing,
destroy KMS keys, erase object storage, remove account rows, or claim those
external actions occurred.

## Trust and observability ceiling

The service promises encrypted storage/backups, owner-scoped access, isolation,
private cell ingress, and content-minimal operations. It does not promise
zero-knowledge or end-to-end encrypted search: cells process plaintext in memory,
and privileged infrastructure operators remain inside the trust boundary.

Central analytics, support, SLO, and audit systems may record opaque account/cell
and request IDs, command name, result/error class, duration, release/protocol,
safe byte/count buckets, entitlement revision, and lifecycle transitions. They
do not record note/source bodies, extracted text, queries, titles, filenames,
vault paths, raw arguments, session/transfer/cell credentials, encryption keys,
or Paddle secrets. Cross-tenant routing failures and credential rejection are
audited without echoing the presented identifier or secret.
