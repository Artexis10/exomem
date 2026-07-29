## Why

Exomem's Hosted OpenAI package is rendered against the real registered application, but its checked-in marketplace validator and public review packet still encode older portal limits and incomplete submission material. That can make a locally green package fail current OpenAI review even though the MCP runtime and universal ChatGPT/Codex plugin are already built.

## What Changes

- Enforce the current OpenAI limits for short descriptions and test prompts instead of accepting the older, longer values.
- Require the OpenAI review packet to carry a recording handoff and explicit justification for tool annotations.
- Reject private-alpha, trial, demo, hypothetical, or not-yet-built language from public OpenAI listing fields without changing Exomem's internal broad-launch policy.
- Replace the stale public listing copy and make positive review cases target a checked-in deterministic generic reviewer fixture with reset semantics.
- Distinguish provider-submission readiness from broad-public activation: a seeded reviewer account can unblock review, while the existing signed public-admission and fresh non-reviewer checks still gate public activation.
- Regenerate and verify the universal OpenAI package and directory packet with the existing registered `asdk_app_*` identity.
- Require signed, secret-free evidence that provider-matched reviewer access is live and fixture-bound, while leaving raw credentials, portal submission, clean-client evidence, publication receipts, and public activation operator-controlled.

## Capabilities

### New Capabilities

- `openai-marketplace-submission-contract`: Current OpenAI Plugin Directory limits, required review materials, public listing language, and deterministic reviewer cases for the universal ChatGPT/Codex package.

### Modified Capabilities

None.

## Impact

- Affects `src/exomem/hosted_plugins.py`, the canonical Hosted marketplace definition and review cases, focused tests, generated OpenAI package/directory artifacts, and hosted-client documentation.
- Reuses the merged Hosted package, MCP tool schemas, signed readiness evidence, and publication state machine; it does not change runtime tools, tenant architecture, package layout, or the Claude channels.
- Depends on the paired Substrate reviewer-access change and the unfinished native-client acceptance tasks in `add-hosted-client-plugins` before an operator can submit or activate the listing.
