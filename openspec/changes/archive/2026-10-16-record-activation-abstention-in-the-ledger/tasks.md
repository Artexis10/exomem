# Tasks

## 1. Bounded observation protocol

- [x] 1.1 Define the shared reason protocol and strict projection; prove unknown reasons cannot enter stored rows.
- [x] 1.2 Bind a fresh activation cell in the existing request context; prove concurrent, nested and cancelled calls cannot exchange observations.

## 2. Wrapper and ledger accounting

- [x] 2.1 Publish the final postfiltered observation and apply outcome precedence; prove real worker-thread calls store abstention correctly.
- [x] 2.2 Preserve `ok` for non-abstained budget-limited packets; prove entry exhaustion, compiler exhaustion and useful partial results differ.
- [x] 2.3 Add the optional schema-1 block before row hashing; prove mixed historical chains verify and telemetry cannot alter responses.
- [x] 2.4 Document the outcome and observation semantics in observability guidance; check that unknown and observed results remain distinct.

## 3. Readers and historical interpretation

- [x] 3.1 Update strict summaries and client classifiers; prove historical unknown coverage remains separate from current activation observations.
- [x] 3.2 Add the narrow historical timing join; prove current inconsistencies and refusal or error mismatches still reject.

## 4. Integrated delivery

- [x] 4.1 Run scoped ledger, wrapper, egress and reader workflows plus privacy, strict OpenSpec and required static gates; retain exact input hashes.
- [x] 4.2 Complete independent exact-content review and integrated CI; deliver the producer and readers through the ordinary release. Evidence: #1694 merged (f80864884) with `required CI gate` passing; first released in v0.116.0; code in `src/exomem/activation_result.py` and `src/exomem/server.py`, tests in `tests/test_activate_context_surface.py` and `tests/test_durable_closure_ledger.py`.
- [x] 4.3 Synchronize the call-ledger delta and archive through OpenSpec after delivery evidence; preserve later requirements and validate strictly. Evidence: delta re-read against current code and the canonical requirement (no later edits to it); archived through `openspec archive`.
