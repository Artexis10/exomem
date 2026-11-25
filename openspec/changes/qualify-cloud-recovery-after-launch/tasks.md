## 1. Recovery drills

These tasks moved from `adopt-exomem-cloud-plain-cells` with their evidence limits unchanged. Each names its old number. Each drill runs on a QA cell, a copy or approved disposable infrastructure, never on a tenant's live data.

- [ ] 1.1 (was 6.6) Resilience 2: qualify a per-cell historical rollback and return through the existing release API.
  Use an exclusive writer window for the QA cell, a fresh post-write pre-attempt backup S1 and a copied vault plus host state.
  Verify old-image route reads/writes, custody and durable debt before any live pin.
  Schema 8 in 0.114 differs from current schema 10; readiness and manifest compatibility IDs do not prove route compatibility.
  A synthetic schema-10 negative probe with the cached 0.114 image refused without changing the store on 2026-10-11.
  It does not prove the restored vault-plus-host copy or successful historical rollback.
  If the copy is incompatible, prove refusal and current-image recovery, avoid the live pin and leave successful historical rollback open.
  Never restore pre-upgrade S0 over later committed writes or add a downmigration.
- [ ] 1.2 (was 6.7) Resilience 3: recheck damaged-volume or bad-import recovery through the existing in-place B2 restore helper.
  The 2026-10-09 QA restore matched 45/45 vault files with 93 seconds of downtime.
  Host-state comparison was absent from that historical in-place run.
  The 2026-10-13 deployed-image scratch recovery matched both roots and verified custody, service authentication and cleanup.
  Scratch recovery does not close the in-place failure path or gateway OAuth acceptance. Keep those scopes open.
- [ ] 1.3 (was 6.8) Resilience 4: prove recovery after actual CSI node loss on approved disposable infrastructure.
  Verify surviving-volume reattachment or B2 recovery, canonical data, host custody and service readiness.
  The older local-storage CI node-loss run does not prove the current CSI deployment's failure path.
  Price any rental before obtaining its authority; no rental is authorized by this task record.
- [ ] 1.4 (was 6.9) Resilience 5: rehearse a server move with the existing backup, stop, volume-release, placement and resume procedures.
  Compare canonical vault and host state, preserve post-backup writes and report downtime after the replacement node exists.
