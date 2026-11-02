## Context

`readiness.retrieval_admission` maps the process state to `ready`, `warming`,
`unavailable` or `unverified`. `find` routes on that state: it serves the
published catalogue in `unavailable` and refuses `warming`. The readiness probe
reports `lagging` only from `unavailable`, and only when `retrieval_proven` says
this managed process made a strict proof since its warm began. Before this
change, a cleared ready bit mapped to `warming` for the whole warm, so a proven
process could not reach `unavailable` until the warm finished.

## Decisions

- **Scope: every managed process with a strict proof in this warm.** The rule
  keys on the existing `retrieval_proven` flag, not on how the process started.
  A promoted-only scope was rejected:
  - It buys no safety. A cold start's guarantee is that nothing reports ready
    before a strict proof by this managed process in this warm. The flag already
    starts every warm, every `manage_runtime` and every reset at false, and a
    standby or unmanaged proof never sets it, so that guarantee is unchanged.
  - A cold process that has proved its catalogue already serves admitted recall
    for the rest of its warm. Serving the same published catalogue under the
    same identity checks, with the lag disclosed, adds no exposure.
  - A cold restart under writes flaps the same way, and for longer, because its
    warm also builds the corpus and loads every model.
  - Promoted-only would make `readiness` read standby state and add a second
    flag to reset at every epoch, as a special case for one caller.
- **One state, one rule.** A proven process whose ready bit is clear maps to
  `unavailable`, checked before the warm-active branch. `find`, the probe and
  `_served_catalogue_lag` then share one state, so health keeps following what
  recall serves. `lagging` stays a payload-only state. `find` needs no change:
  its warm gate refuses only `warming`.
- **Re-proof follows the proof.** A failed or missing proof may be re-proved and
  published after the required warm stages, as before, or once this process has
  proved its catalogue in this warm. The reason to wait was that the warm's own
  catalogue step heals an adopted catalogue and rebases its lineage before it
  admits. That step has already run when the flag is set, and every other
  managed proof comes from a repair or a bounded apply in this lineage.
- **Cache unchanged.** The ready-proof cache never reuses `lagging`, and every
  re-admission moves the generation it watches.

## Risks

- While a proven process lags during the warm, each probe and each unadmitted
  request runs the strict proof. Concurrent callers share one proof flight. The
  cost matches what the same process pays after its warm.
- A lag-only catalogue repair can now be scheduled while the warm still runs.
  Admitted recall and bounded writes could already schedule one in that period.
- A proven process whose published catalogue cannot be served during the warm
  refuses with `temporarily_unavailable` instead of `warming`. Both are
  retryable, and it is the status the same process returns after its warm.
