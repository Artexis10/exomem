## Why

A hosted candidate selecting `hosted-alpha-agent-v4` provisions a healthy runtime that serves only v1: the runtime infers its profile from a Records feature flag, and provisioning never passes the selected profile. Its authenticated v4 contract request therefore fails with `HOSTED_SURFACE_PROFILE_UNSUPPORTED`, preventing cell binding.

## What Changes

- Carry the selected runtime target's profile through the cell chart into trusted runtime configuration.
- Validate explicit profiles against the existing canonical registry and serve exactly the selected profile.
- Preserve the v1/v2 fallback for deployments that omit explicit selection and retain independent Records lifecycle gates.
- Exercise profile selection through real configuration and authenticated routes, without overriding the selection property.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `hosted-agent-surface`: the runtime must serve the profile selected by its trusted deployment configuration.

## Impact

Hosted runtime configuration, provisioner chart values, the cell Helm chart, and their regression tests. No public request field, profile membership, credential, database schema, or promotion policy changes. Rollout requires rebuilt runtime and provisioner images with a verified deployment composition.
