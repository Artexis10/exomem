## 1. Source adoption

- [x] 1.1 Add red-first matrix/source tests, implement the bounded BWS reader and verify no fallback, no plaintext diagnostics and no access on dry-run.
- [x] 1.2 Record the existing production control-key binding and document its BWS-backed route; verify exact matrix/binding agreement and a read-only shared-helper check.

## 2. Delivery

- [x] 2.1 Run hosted handoff/infrastructure tests, privacy and strict spec validation; obtain independent security review and deliver a ready PR with evidence, closing OpenSpec after shipped completion.

Delivery: PR #1119 merged as `be382efced5dbc7732185f31eb46b2aedad37202` after successful core and hosted-infrastructure CI at `36fce1e458cdafb57fc350cb14e824024b757ba9`. This closes source adoption, not gateway deployment or rotation of unrelated credentials.
