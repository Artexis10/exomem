## 1. Local upload sizing and raw protection

- [x] 1.1 Give direct preserves and captures on local ingress their own upload cap, and keep holds within the redemption cap.
  Evidence: `tests/test_local_ingress_e2e.py::test_threat_scenarios_hold_through_a_real_worker_behind_the_real_ingress` preserves 1,024 bytes over a 64-byte public cap through the real worker, and refuses the same bytes on the public listener.
- [x] 1.2 Accept `raw_protection` on `/upload` and `exomem attach --raw-protection`, and refuse it on a hold.
  Evidence: the same end-to-end test checks the owner-only marker and the hold refusal; `tests/test_local_ingress_cli.py::test_attach_asks_for_an_owner_only_original_and_a_hold_refuses_the_flag` checks the flag and its refusal without a scope.
