## Why

Durable knowledge silently outgrows the page it is written into. A compiled note can begin as one coherent subject and, through a sequence of individually defensible writes, accumulate several unrelated durable topics until it is no longer the right canonical home for any of them. Nothing in the write path notices. Today only a user who understands note types, project scope and hub structure can catch it, which contradicts the product thesis that governed structure should feel effortless.

The runtime already parses every semantic unit and holds the page's declared identity in memory at commit time, so the condition is observable without new infrastructure. What is missing is a bounded, advisory signal that says the current destination has stopped being coherent, so the agent can raise it in ordinary language and offer to organise it.

## What Changes

- Add a deterministic, local, read-only detector that decides whether a just-written compiled page shows recurring durable material outside its own declared scope.
- Return at most one advisory `structure_suggestion` on successful compiled-note mutations from `remember`, `edit_memory`, `observe_memory`, and `replace_memory`, carried on the existing commit results and projected into the default compact terminal.
- Report `strength` as `strong` or `moderate` with sorted deterministic reason codes, and never a numeric confidence.
- Require convergent evidence: a single weak signal never produces a suggestion, and raw page length is never an input.
- Restrict every reported fact to the page the caller just wrote, so no other page's path, title, or count can be inferred from the suggestion.
- Exclude non-compiled material, navigational pages, and pages whose declared identity already announces breadth.
- Keep the analysis advisory: any detector failure, refusal, or absent optional state leaves the committed mutation and its terminal unchanged.
- Teach bootstrap and the canonical workflow skill how to inspect and present a suggestion, and correct the existing post-write instruction that names response fields the default response does not carry.

## Capabilities

### Modified Capabilities

- `command-surface`: successful compiled-note mutations may carry one bounded advisory structural suggestion in the default compact terminal, without changing mutation identity, status, or existing keys.
- `agent-bootstrap-contract`: bootstrap and the canonical write loop teach agents to inspect a structural suggestion, surface a strong one in domain language, ask before restructuring, and stay quiet on a moderate one.

## Impact

The compiled-write commit results, the compact terminal projection, the live Records acceptance allowlist, the bootstrap post-write guidance, and the scaffold skill and operations reference change, plus a new pure detector module and its focused tests. Regenerated Claude plugin skill copies follow their existing packaging path.

No MCP tool is added. No tool description, parameter, or input schema changes, so the packaged tool-surface fingerprint and the external connector attestation are untouched. No new table, index, background worker, embedding call, persistent review object, graph schema change, or automatic file or project migration is introduced. Existing write-latency, governance, and mutation-safety gates continue to apply unchanged.

Deliberately out of scope: proposing a destination page or project, naming an existing target, persisting accept or dismiss state, cross-session cooldown, whole-corpus structural audits, and any migration preview or execution.
