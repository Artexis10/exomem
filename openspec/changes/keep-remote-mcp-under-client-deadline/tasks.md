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
- [ ] 4.2 Reproduce a slow read and governed write through the stable connector without passive replay or a false failure.
- [x] 4.3 Remove the stale duplicate glioma note through governed trash semantics and verify the intended full note remains.
