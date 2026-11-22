# Store encrypted original replicas in Backblaze B2

## Why

A filesystem replica can share a failure domain with the vault and its local archive. A version-addressed B2 copy gives the owner another place to recover an original.

## What Changes

- Add a declared B2 replica store using the existing native B2 transport.
- Bind each replica to the provider's exact file version and its declared ciphertext digest.
- Use the existing content check to verify ciphertext and recovered plaintext.
- Exercise the adapter with synthetic data in an isolated sandbox bucket.

## Capabilities

### New Capabilities

- `b2-original-replicas` — Immutable encrypted replica custody through a restricted B2 bucket and prefix.

### Modified Capabilities

## Impact

The media store accepts an optional B2 replica adapter and reads its credential at runtime. Existing replica commands and scheduled verification use the adapter.
