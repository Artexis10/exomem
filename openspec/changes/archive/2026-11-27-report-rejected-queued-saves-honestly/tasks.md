## 1. Honest queued saves

- [x] 1.1 End a replay that rejected rows and committed nothing as `failed` with the rejected rows.
- [x] 1.2 Read a save stored `applied` that committed nothing as not saved.
- [x] 1.3 Refuse malformed or sourceless bulk rows under `abort` before queueing, naming every row, from one shared row check; direct calls keep the per-row report.
- [x] 1.4 Return arguments from a status read only for a failed save.
- [x] 1.5 Add the bulk row example to the operation help.
- [x] 1.6 Tests: a queued all-rejected bulk write fails and its status has no payload while queued; malformed rows behind the queue are refused at once; the taught example passes the row check.
