# Hosted maintenance Lease release compatibility

## Context

The provisioner uses a Kubernetes `coordination.k8s.io/v1` Lease to serialize
cell maintenance. Kubernetes Python client 35 makes the Lease deletion `body`
argument keyword-only, but `KubernetesMaintenanceLeaseAdapter.release` passes
it positionally. The worker therefore crashes after completing maintenance and
before releasing the Lease.

The existing unit fake accepted a positional body, so it did not reproduce the
generated client's call contract.

## Decision

Pass the unchanged UID and resource-version precondition document as the
keyword argument `body`. Change the test double to require the same
keyword-only signature as Kubernetes 35.

Do not change Lease ownership, expiry, acquisition, retry, checkpoint, or
provider-operation semantics. Relaxing deletion preconditions, catching the
`TypeError`, or replacing the Kubernetes client would widen the change without
addressing the incompatible call shape.

## Verification

The focused test must fail against the old adapter with the production
`TypeError`, then pass with the keyword call while proving the exact deletion
preconditions are preserved. The full provider adapter and lifecycle suites,
Ruff, package build, and repository validation must remain green before the
patched provisioner image is published.

## Success criteria

Maintenance release calls the Kubernetes 35 client successfully, deletes only
the owned Lease version, and leaves all existing maintenance serialization and
provider recovery contracts unchanged.
