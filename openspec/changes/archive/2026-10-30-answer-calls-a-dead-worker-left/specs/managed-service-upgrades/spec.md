## ADDED Requirements

### Requirement: A call whose worker dies is answered as retryable

When the worker dies after the ingress forwarded a call, and the call's request identity is available, the ingress SHALL answer the call with a protocol-compatible error instead of a truncated response. The error SHALL carry the call's request id, say that the call may be retried unchanged, and mark itself retryable. The ingress SHALL NOT replay the call itself. A response whose partial body cannot carry an error MAY end without one.

#### Scenario: The worker dies while streaming an answer
- **WHEN** the worker dies after it started an event-stream response to an MCP call
- **THEN** the client receives a final event with a JSON-RPC error for that call's id, marked retryable

#### Scenario: The worker dies before answering
- **WHEN** the worker dies after reading an MCP call but before sending response headers
- **THEN** the client receives a JSON-RPC error for that call's id, marked retryable
