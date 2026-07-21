## ADDED Requirements

### Requirement: Edge-Transit Stamp

The HA edge worker SHALL attach a per-request transit proof to every request it
proxies to an origin — on both the read fan-out path and the mutation path —
consisting of a request identifier header and an HMAC header computed over that
identifier with the shared coordinator secret. An origin with writer-lease
coordination enabled SHALL refuse Cloudflare-transited unsafe-method requests
(any method other than GET/HEAD/OPTIONS) that lack a valid transit proof with
the terminal error `INGRESS_BYPASSED`, and SHALL
leave non-Cloudflare (local) traffic unaffected. Enforcement SHALL be
disableable via `EXOMEM_EDGE_STAMP_ENFORCE=0` without redeploying.

#### Scenario: Tunnel-direct mutation is refused loudly

- **WHEN** a mutation-capable POST reaches an origin through a Cloudflare
  tunnel without transiting the HA edge worker
- **THEN** the origin refuses it with `INGRESS_BYPASSED` before lease
  evaluation
- **AND** the refusal names ingress (DNS binding, tunnel ingress, worker route
  coverage) rather than surfacing a writer-lease error

#### Scenario: Edge-proxied traffic passes

- **WHEN** the worker proxies any request to an origin
- **THEN** the request carries a request-id and a valid HMAC transit proof
- **AND** the origin serves it exactly as before this change

#### Scenario: Local traffic is exempt

- **WHEN** a request without a `cf-ray` header reaches the origin (CLI, REST on
  localhost, health probes)
- **THEN** no transit proof is required

#### Scenario: Break-glass

- **WHEN** `EXOMEM_EDGE_STAMP_ENFORCE=0` is set on an origin
- **THEN** requests lacking a transit proof are served
- **AND** each such request is still logged content-free as a bypass

### Requirement: Edge Deploy Provenance

The HA edge worker SHALL expose an authenticated `GET /__version` endpoint
returning its deploy identity (git SHA when supplied at deploy time,
`"unlabeled"` otherwise) and its effective non-secret routing variables,
including the mutation timeout, coordination requirement, replica ids, and
origins. The coordinator secret SHALL never appear in the response.
Unauthenticated requests SHALL receive the standard 401 envelope.

#### Scenario: Doctor detects a stale or misconfigured edge

- **WHEN** `exomem doctor` fetches `/__version` from the public base URL
- **THEN** it fails the ingress check if the endpoint is absent (apex not
  served by the worker)
- **AND** it reports drift when the effective mutation timeout is below 60
  seconds or the replica-id/origin mapping does not include this origin

#### Scenario: Unlabeled deploy is visible

- **WHEN** the worker was deployed without the deploy helper supplying a git
  SHA
- **THEN** `/__version` reports `"git_sha": "unlabeled"`
- **AND** doctor surfaces this as a warning, not a failure

### Requirement: Ingress Conformance Check

`exomem doctor` SHALL verify, on lease-enabled deployments, that the public
base URL is served by the HA edge worker (worker-shaped 401 on unauthenticated
coordinator paths and a responsive `/__version`), that the public
`/health/ready` replica matches the coordinator's current lease holder, and
SHALL warn when the configured lease TTL is below 30 seconds. The checks SHALL
be read-only and skipped entirely when coordination is disabled.

#### Scenario: Apex served tunnel-direct

- **WHEN** the public base URL responds to `/__version` with anything other
  than the worker's authenticated payload
- **THEN** doctor fails the ingress check and names the three usual suspects
  (DNS binding, tunnel ingress hostname, worker route coverage)

#### Scenario: Read routing disagrees with the lease

- **WHEN** the public `/health/ready` reports a replica other than the
  coordinator's current holder
- **THEN** doctor reports the divergence with both identities
