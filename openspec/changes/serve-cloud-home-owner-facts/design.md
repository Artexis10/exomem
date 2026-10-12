# Design

## Context

See `proposal.md` for the gaps.
Home reaches a cell only through the Cloud gateway and the cell's stateless `/mcp` endpoint.
The gateway authenticates the owner, derives the cell bearer and forwards an allowlist of request headers.
A cloud cell resolves that bearer to the cell owner.

## Goals / Non-Goals

Serve the facts Home needs as MCP output fields, with unknown values as null.
Add no route, no model and no write path. Keep every new field free of content.
The gateway's own change and the Home page are out of scope.

## Decisions

### One precedence rule

`prominence.resolve_context(context)` holds the only precedence rule: environment, the context's saved value, the identity-wide saved value, machine config, the unreadable-record floor, then the context default.
`resolve` and the active-source report both answer through it, so the level a client gets and the level Home shows cannot disagree.
The context is a parameter, so one request can report both contexts. It still tunes eagerness only.
Setting and clearing one context, with the revision compare-and-set, already exist in `configure_memory`; this change reuses them.

### Released items come from a release set, not the receipt key

Measured on a cloud cell with the default empty policy: `ask_memory` returned five notes while the disclosure collector's `path_outcomes` stayed empty.
That set is the receipt's dedupe key, and the empty-policy shortcut releases without a receipt.
The collector therefore keeps a separate set of released identities, filled wherever a release decision is made, whatever the policy.
The release decisions are the receipt schema's closed set (`released`, `release_authorized`).
The outermost read boundary of a call reports the size of that set.

### Access and write targets

`access` is the classification the dispatcher already made for the call, carried to the ledger row through the call token, like committed targets.
A write's `item_count` is the number of distinct targets the call addressed or reported committing.
At the commit, the dispatcher reports the pages that the committed terminal names (`path` or `paths`), so a write whose destination the server chose, such as `remember`, still has a count.
A write whose arguments name no page and whose terminal names none has no known target, so its count is null.
A call that did not succeed has a null count.

### Activity

The action comes from the command registry's `product_actions` and the row's `access`: a read that asks is `recalled`; a write that saves, records or updates is `saved`.
Rows of one client family and action join the newest event of that pair when they are at most 60 seconds older than its newest row.
An event's `items` is null when any joined row's count is null.
`since` is the oldest row the returned list accounts for, or the ledger writer's start when there is none.
A version 1 row ends the readable history, because it cannot say whether it read or wrote.
The section is owner-only: it shows when and how much the owner's other assistants used memory.
A disabled or unreadable ledger is the error `ACTIVITY_UNAVAILABLE`, never an empty list.

### Note count

The count is the existing compiled-note count, or null when a read fails.
A cloud cell caches it against the writer commit counter: every change to its vault goes through a governed writer.
A local install counts every time, because an editor can change the vault outside the writers.

### The gateway's client header

Measured with a real stateless request through a cloud-mode server: a handshake-protocol `tools/call` with the gateway's `User-Agent` resolved to surface null and the coding context.
The same call with a client-naming `User-Agent`, or with a per-request `clientInfo` from the 2026-07-28 protocol, resolved to the conversation context.
On a cloud cell, the caller's client name reads `x-exomem-oauth-client` after `clientInfo` and before `User-Agent`.
Only a cloud cell trusts it: there the cell bearer proves the gateway sent the request, and the gateway forwards no caller copy of the header.

### The registry decides a Cloud client's context

Surface detection matches only some client names, so the web could know an app's context only by copying that name list.
On Cloud, the control plane's client registry decides instead: it derives each client's context from data it already holds, and one function serves both the gateway and the web.
The gateway sends that context in `x-exomem-client-context`, next to the client name.
A cloud cell honours `coding` or `conversation` there, after the operator's surface override and before name detection.
It ignores any other value, because the header tunes eagerness only and detection still has an answer.
Home then saves a level to exactly the context the cell applies to that app, before the app's first call.

## Risks / Trade-offs

A local install still depends on surface detection, which matches only some client names.
Saves whose destination the server derives show unknown item counts until those writers report committed targets.
