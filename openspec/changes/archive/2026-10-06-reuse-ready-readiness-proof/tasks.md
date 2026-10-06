## 1. Reuse a ready readiness proof

- [x] 1.1 Serve `/health/ready` from a ready proof for up to 30 seconds, never from a not-ready, failed or standby proof, and void it on an admission change or a standby promotion. Evidence: `tests/test_health_probe_cost.py`.
- [x] 1.2 Report `proof_age_seconds` on every readiness answer. Evidence: `tests/test_health_probe_cost.py`.
