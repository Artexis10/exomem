## 1. Establish the failure precisely

- [ ] 1.1 Split the two arms of the condition so the message distinguishes "no predecessor at this key" from "predecessor hash differs", and confirm from a nightly run which one fires.
- [ ] 1.2 Trace what produces `item_identity` and compare it, byte for byte, with the `relative` the lookup uses.
- [ ] 1.3 Reproduce on any platform by forcing the mismatch the trace reveals; only fall back to a Windows host if no such reproduction exists.

## 2. Fix

- [ ] 2.1 Make the predecessor match hold on every declared platform, comparing identities the way they are minted rather than relaxing the comparison.
- [ ] 2.2 Confirm the colliding-membership refusal still refuses.

## 3. Stop it hiding

- [ ] 3.1 Cover the reproduction in a test that runs on the required pull-request gate, not only in the advisory nightly matrix.
- [ ] 3.2 Confirm the nightly Windows shards clear `tests/test_governance_active_tuple.py`.

## 4. Check the neighbours

- [ ] 4.1 Determine whether `test_graph_epoch_protocol.py::test_protocol_artifact_reader_rejects_replace_during_guarded_read` and `test_graph_lifecycle_windows.py::test_windows_private_unlink_closes_delete_pending_handle_before_flush` share this cause or are separate.

## 5. Verify

- [ ] 5.1 Run the governance and graph suites plus lint.
- [ ] 5.2 Run `openspec validate fix-catalog-identity-on-windows --strict` and `openspec validate --specs --strict`.

## 6. Closure

- [ ] 6.1 Once merged and therefore demonstrably shipped, sync the delta into `openspec/specs/` and archive with `openspec archive`.
