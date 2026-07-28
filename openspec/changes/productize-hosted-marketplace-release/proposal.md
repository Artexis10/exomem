## Why

The hosted runtime and native Claude/OpenAI packages are merged, but they are still pending release: production discovery and legal URLs are not healthy, the OpenAI registration contract has moved to the universal Plugin Directory, and neither platform has clean-client promotion evidence. Exomem needs one fail-closed marketplace release path that turns those existing artifacts into an install-and-login product without claiming availability before the live system proves it.

## What Changes

- Align the OpenAI candidate with the current universal ChatGPT/Codex plugin contract while preserving the registered `asdk_app_*` package identity, using platform-valid MCP wiring, and tracking the later portal-issued `plugin_asdk_app_*` directory identity separately.
- Add a canonical, deterministic marketplace submission definition and generate review packets for the Claude Connector Directory, Claude plugin directory, and OpenAI Plugin Directory from it.
- Add an executable readiness gate that verifies exact promoted artifact bindings, signed and fresh public-surface evidence, full tool contracts, reviewer account instructions, and platform test cases without committing credentials or tenant identifiers.
- Track append-only submission revisions and active publications separately from runtime artifact promotion so an update can be reviewed while the prior release remains public and a draft, rejected, or stale listing can never make a pending package appear live.
- Gate public submission on a signed public-admission attestation covering ordinary account acquisition, capacity, quotas, abuse controls, spend alarms, support readiness, and the final public pricing decision; a seeded reviewer account is not launch proof.
- Replace manual hosted-client instructions with the intended install, sign in once, and use Exomem journey, while keeping custom instructions as a clearly labelled fallback for chat surfaces that do not activate bundled skills.
- Keep actual directory submission and publication as operator actions gated on the existing clean-account, cross-client, isolation, and independent-review acceptance requirements from `add-hosted-client-plugins`.

## Capabilities

### New Capabilities

- `hosted-marketplace-release`: Deterministic platform submission packets, public-readiness validation, release-state separation, and an install-first hosted product journey for Claude, ChatGPT, and Codex.

### Modified Capabilities

None.

## Impact

- Affects `src/exomem/hosted_plugins.py`, `scripts/hosted-plugin.py`, hosted plugin definitions/generated artifacts, focused tests, and hosted-client documentation.
- Depends on the merged hosted client packages and Substrate OAuth/control-plane implementation; no second MCP server, tenant model, or package architecture is introduced.
- Requires a paired Substrate deployment/product-surface change for healthy OAuth discovery, hosted onboarding, privacy, and terms routes.
- Public submission additionally requires operator-held platform access, a verified publisher identity, a populated reviewer account, the registered OpenAI package app identity, and the portal-issued directory identity when a provider has assigned one.
