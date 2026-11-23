## 1. Issue the handoff

- [x] 1.1 Add the typed Source size to `upload_files`. Verify the one-shot limit, configured session maximum and hosted refusal.
- [x] 1.2 Bind the digest, size, deployment and issuing login to a one-use resumable ticket. Verify that a ticket cannot cross upload routes.

## 2. Resume and publish

- [x] 2.1 Admit a Source session under its issuing principal. Persist its authority in the separate session namespace and share disk admission.
- [x] 2.2 Verify the issuing login on session requests and at governed publication. Keep unreadable authorization retryable.
- [x] 2.3 Recover a full part left in receiving state. Reuse the existing Source mutation identity for replay.

## 3. Client and delivery

- [x] 3.1 Extend the attach handoff path to stream from a held file and resume at the durable offset.
- [x] 3.2 Prove one original above the one-shot limit through the real tool and attach route. Interrupt the transfer and restart before publication.
- [x] 3.3 Verify one Source receipt with the exact digest, and verify that revocation refuses the pending Source after restart.
- [x] 3.4 Verify small handoff compatibility, generated tool contracts, strict OpenSpec, scoped tests and the public artifact gate.
