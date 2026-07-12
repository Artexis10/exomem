## 1. Routing regression coverage

- [x] 1.1 Add pure Worker tests that classify single and batched MCP `tools/call` requests conservatively.
- [x] 1.2 Add tests proving an active-holder tool call uses the long timeout and is never replayed to the passive origin.
- [x] 1.3 Add tests proving no-holder health selection sends a tool call exactly once to the healthy replica.
- [x] 1.4 Preserve short-timeout fallback coverage for discovery and initialization traffic.

## 2. HA edge implementation

- [x] 2.1 Add separate short-connectivity and MCP-tool timeout configuration.
- [x] 2.2 Route active-holder tool calls to one origin only and fail closed on ambiguous timeout/error.
- [x] 2.3 Select one healthy origin before a no-holder tool call while preserving normal safe-request fallback.

## 3. Deployment parity

- [x] 3.1 Document the same-release requirement and commands that compare repo and installed-package versions on both replicas.
- [x] 3.2 Upgrade and restart the desktop and laptop services on the same release/fix build.

## 4. Verification and cleanup

- [x] 4.1 Run Worker tests, focused Python transport tests, Ruff, and strict OpenSpec validation.
- [x] 4.2 Reproduce a slow read and governed write through the stable connector without passive replay or a false failure.
- [x] 4.3 Remove the stale duplicate glioma note through governed trash semantics and verify the intended full note remains.

### Live connector evidence (2026-07-12)

- Authenticated deep hybrid read through `https://exomem.substratesystems.io/mcp`: 4.22 s client-observed / 3.82 s server-side, successful, zero laptop MCP POSTs.
- Governed `remember` through the same public MCP route: 6.16 s client-observed / 5.72 s server-side, successful, exactly one note created, zero laptop MCP POSTs.
- Worker version under test: `5c2ade44-1b5b-4ef2-8c26-e5879ac12c50`.
