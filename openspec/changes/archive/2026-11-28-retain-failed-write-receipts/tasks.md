## 1. Retain terminal identities

- [x] 1.1 Keep a failed receipt addressable by its original write identity. Verify a same-identity resend after restart does not execute the write.
- [x] 1.2 Leave unproven effects unresolved in the receipt and next-save notice. Verify a later save gives the failure count once and directs the caller to the receipt.
- [x] 1.3 Bound the indexed restart lookup to the retry window. Check the query plan with a large expired history, and verify dismissal of a legacy duplicate leaves an older receipt addressable.

## 2. Verify the release

- [x] 2.1 Install the released wheel in disposable state. Verify the same-reference behaviour against a failure receipt written by the previous version.
