# Tasks

## 1. Keep passage queries off the capture's model turn

- [x] 1.1 Run a passage query encode in its own execution lane on a concurrent encoder, with admission from the shared budget. Verify: a passage request returns its hit while another encode holds the shared admission and turn.
- [x] 1.2 Leave the model-turn wait when the caller leaves. Verify: a request queued behind another encode releases its admission while that encode still runs, and encodes nothing.
- [x] 1.3 Measure one and four callers, idle and during a capture, in the production envelope before and after the change, with capture duration.

## 2. Closure

- [x] 2.1 Synchronize the delta into the canonical spec and archive this change with `openspec archive`. Verify: `openspec validate --all --strict` passes before and after the archive.
