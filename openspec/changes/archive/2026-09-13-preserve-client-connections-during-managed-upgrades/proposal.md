## Why

An MCP call made while a service upgrade stops the listener can permanently break an already connected client, even though Exomem already uses stateless HTTP and durable OAuth sessions. Upgrades must preserve the public connection and finish active operations so users can continue the same conversation without refreshing or reconnecting.

## What Changes

- Add a managed Linux/WSL service mode with a persistent HTTP ingress and one replaceable Exomem worker.
- Stage release environments while the existing worker serves, then drain finite requests, stop the old worker and its descendants, run bounded offline preparation, and admit the verified replacement.
- Hold new requests within explicit memory, count and time bounds during handoff. Never replay an operation that was forwarded to a worker.
- Preserve authenticated standalone MCP GET/SSE connections across worker replacement, and preserve the existing OAuth store, issuer and service environment.
- Add operator status and upgrade controls on a private local socket, with durable transition records and safe recovery after failure.
- Wire the managed path into the Linux installer and upgrade script. Existing direct HTTP, stdio, Windows/macOS and Hosted deployment paths retain their current contracts.
- Document the one-time migration from a direct listener and the limits: supervisor/host restarts, unbounded migrations and client timeouts shorter than the handoff budget cannot promise connection continuity.

This is transport and process lifecycle work; it adds no model capability or heavy optional dependency. Managed mode is explicitly enabled at installation. Upgrade preparation or drain failure leaves the current worker serving; failures after shutdown retain a recovery record and never silently restart an incompatible release.

## Capabilities

### New Capabilities

- `managed-service-upgrades`: Persistent ingress, bounded admission, sequential worker replacement and operator recovery for a managed Linux/WSL service.

### Modified Capabilities

- `mcp-session-continuity`: Existing authenticated clients remain usable through a managed worker upgrade, including calls arriving during the handoff and active response streams.

## Impact

New service ingress, process manager and operator CLI modules; the server's private worker transport; Linux service templates and installation/upgrade scripts; isolated protocol, lifecycle and service-script tests; deployment documentation. Public tool schemas and vault mutation logic are unchanged.
