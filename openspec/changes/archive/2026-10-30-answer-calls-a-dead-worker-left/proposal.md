## Why

When the service worker dies while a call is in flight, the ingress has already started relaying the worker's response. The broken relay escapes the ingress, and the client receives a truncated response that MCP clients report as `-32603 Internal error`. The client cannot tell whether to retry, and a write's outcome looks unknown.

## What Changes

- A forwarded call whose worker dies before answering receives a JSON-RPC error with its request id, code `-32000` and `data.status: "retryable"`. The error says to retry the identical call and that a write is never applied twice.
- An event-stream response carries the error as a final event. A call whose worker died before sending headers gets the error as a JSON body.
- A partial JSON body cannot carry an error, so that case still ends as before.

## Impact

- Affected spec: `managed-service-upgrades`.
- Affected code: `src/exomem/service_ingress.py`.
