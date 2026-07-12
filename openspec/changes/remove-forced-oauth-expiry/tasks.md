## 1. Regression Coverage

- [x] 1.1 Add a focused OAuth construction test proving no fixed fallback access-token expiry is passed.
- [x] 1.2 Preserve assertions for the existing verifier, signing key, and shared-storage wiring.

## 2. OAuth Lifetime Fix

- [x] 2.1 Remove Exomem's forced 30-day `fallback_access_token_expiry_seconds` override.
- [x] 2.2 Add a concise code comment explaining why provider-aware FastMCP behavior is required for GitHub OAuth Apps.

## 3. Verification

- [x] 3.1 Run focused OAuth tests and Ruff on changed Python files.
- [x] 3.2 Run strict OpenSpec validation and the lean test suite in proportion to the change.
