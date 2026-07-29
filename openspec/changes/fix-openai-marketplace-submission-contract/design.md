## Context

The Hosted package and marketplace state machine were built against an earlier OpenAI submission surface. The package used the legacy bare `asdk_app_*` form instead of the current registered `plugin_asdk_app_*` technical identity, while the canonical definition also allowed a 200-character OpenAI short description and 300-character prompts, carried private-alpha release language, and rendered tool annotations without the explanation material the review form asks for. The packet also treats a seeded reviewer and broad ordinary-user admission as one readiness concept, even though provider review can use an existing pre-provisioned account.

This change is intentionally a release-contract correction. It must not reopen the MCP runtime, command schemas, plugin layout, tenant model, or Claude channel architecture. Operator-held reviewer credentials, raw recording URLs, provider identities, and portal receipts remain outside Git.

## Goals / Non-Goals

**Goals:**

- Make local validation match the current OpenAI Plugin Directory field limits and required review material.
- Produce deterministic, secret-free review cases tied to a versioned generic reviewer fixture.
- Remove trial/private-alpha language from the public OpenAI listing while retaining honest Hosted boundaries.
- Let an operator prepare and submit a reviewer-ready candidate before broad admission is enabled, without weakening Exomem's own public-activation gate.
- Preserve one universal OpenAI package for ChatGPT and Codex and the existing registered package identity.

**Non-Goals:**

- Public self-service signup, pricing, capacity allocation, or subscription sales.
- Automatic provider portal submission, recording upload, or publication.
- A new MCP server, MCP App UI, runtime tool, native-memory integration, or client-specific OpenAI package.
- Changes to Claude Connector or Claude Plugin limits except shared fixture-safe validation.

## Decisions

### Enforce provider limits in the canonical validator

`load_marketplace_definition` will apply OpenAI-specific limits of 30 characters for the short description and 128 characters per starter prompt. Tests will pin the exact boundaries. Public OpenAI fields will be scanned with word/phrase-aware patterns for private alpha, trial, demo, hypothetical, and not-yet-built claims.

This keeps invalid material from entering generated packets. Relying on a portal rejection was rejected because it makes the deterministic release gate knowingly weaker than the submission contract.

### Version and hash the canonical generic reviewer fixture

A checked-in, generic fixture payload will contain the non-sensitive note keys, titles, exact content, stable references, reset behavior, fixture version, and canonical payload digest required by the positive review cases. Review cases will declare fixture references, and validation will reject unknown references, digest drift, or a fixture-version mismatch. It contains no live reviewer tenant, account, token, invitation, tenant export, or production evidence.

Write-capable cases use a named disposable fixture note and declare how it is restored so retries are deterministic. The checked-in payload defines the expected content but does not pretend a live tenant was seeded; live seeding and reset remain operator actions proved through signed evidence and native-client acceptance.

### Render annotation explanations and a recording handoff, not secrets

Every OpenAI tool entry will include deterministic justifications for its boolean MCP annotations. The OpenAI packet will also declare that a walkthrough recording is required and operator-supplied. The signed prerequisite evidence will gain an OpenAI-only `review_recording_prepared` Boolean; the raw recording URL remains a manual portal input and is never committed.

Storing an unlisted recording URL in the deterministic packet was rejected because it unnecessarily turns an operator handoff into durable public repository data.

### Require live provider-matched reviewer evidence

A separate signed `directory-reviewer-access` evidence document will bind each channel to the trusted deployment SHA, provider (`openai` or `anthropic`), fixture version and payload digest, enabled feature state, active credential state, and credential expiry. It contains no raw username, password, user/tenant ID, or content. Submission readiness requires the evidence to be fresh and the credential expiry to extend through a defined minimum review window.

A generic `reviewer_seeded` Boolean alone was rejected because it can remain true while the feature is disabled, the credential is expired, or the live fixture no longer matches the packet.

### Split submission readiness from broad-launch readiness

`directory_status` will expose `submission_ready` and `submission_blockers` for the provider-required candidate gate. That gate includes the provider-matched reviewer-access evidence above. Existing `ready` and `blockers` retain the stronger broad-launch meaning for compatibility and continue to include signed public-admission evidence.

Transitions to `submitted`, `in_review`, and `approved` require `submission_ready`. A transition to `published` still requires the stronger broad-launch gate, and activation still requires fresh non-reviewer post-install evidence. This allows review with a dedicated existing account while ensuring Exomem never marks a reviewer-only release as publicly activated.

Removing the public-admission check entirely was rejected because hosted cost, abuse, support, and pricing controls are Exomem's product launch boundary even when they are not a provider review requirement.

## Risks / Trade-offs

- [Provider forms drift again] -> Keep exact limits and required material in focused tests and re-check official submission documentation immediately before portal submission.
- [Public copy overstates general availability] -> Describe the live product and governed storage boundary without claiming open signup; retain the stronger activation gate and truthful setup page.
- [A Boolean falsely implies the recording is useful] -> Treat it only as operator-signed submission evidence; native-client review cases and provider review remain authoritative.
- [Fixture payload leaks private data] -> Keep it deliberately generic and run the existing hosted public-input, secret, and scaffold leak guards; never derive it from a live vault.
- [Submission reports ready while reviewer login is broken] -> Require fresh signed provider/deployment/fixture/expiry-bound reviewer-access evidence in addition to native clean-client proof.
- [New readiness keys break callers] -> Preserve `ready`, `public`, and `blockers` semantics; add submission-specific fields rather than renaming existing fields.

## Migration Plan

1. Add failing boundary, review-material, fixture, language, and staged-readiness tests.
2. Update the canonical definition, fixture payload, review cases, and validation/rendering code.
3. Regenerate the OpenAI package, lock files, archive, and directory packet with the registered application ID.
4. Deploy the paired reviewer-access change, seed/reset the declared fixture, prepare the recording, and create signed prerequisite plus reviewer-access evidence.
5. Complete clean ChatGPT and Codex acceptance before portal submission; enable broad admission only through the separate product decision and existing activation evidence.

Rollback is a listing/artifact rollback: restore the prior canonical inputs, regenerate, and keep any provider revision non-active or withdrawn through the existing state machine. Runtime and tenant data are unchanged.

## Open Questions

None. Raw provider form values and credentials are intentionally resolved during the operator submission handoff rather than in source control.
