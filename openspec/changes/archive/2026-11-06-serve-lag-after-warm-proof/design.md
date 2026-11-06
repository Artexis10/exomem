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
    starts every warm, every `manage_runtime` and every reset at false. Only a
    proof that a managed process makes after its warm began sets it, so neither
    a standby's proof nor a proof made during the recall-seed wait before the
    warm counts, and that guarantee is unchanged.
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
- **The required-stage arm stays.** A warm can reach its required stages with
  no admission at all, for example when a repair's proof loses the generation
  check to a warm transition. Only that arm then re-admits before the warm
  finishes, so the proof flag does not replace it.
- **Cache unchanged.** The ready-proof cache never reuses `lagging`, and every
  re-admission moves the generation it watches.

## Archive order

The active change `keep-recall-available-during-pending-recovery` modifies the
same recall-read-path requirement and adds scenarios of its own. Neither block
holds the other's new scenarios. Whichever change archives second must merge
its block with the canonical requirement and keep the other change's
scenarios: here, "A warm that proved its catalogue serves a later lag" and "A
warm that has not proved its catalogue still refuses".

## Risks

- While a proven process lags during the warm, each probe and each unadmitted
  request runs the strict proof. Concurrent callers share one proof flight. The
  cost matches what the same process pays after its warm.
- A lag-only catalogue repair can now be scheduled while the warm still runs.
  Admitted recall and bounded writes could already schedule one in that period.
- A proven process whose published catalogue cannot be served during the warm
  refuses with `temporarily_unavailable` instead of `warming`. Both are
  retryable, and it is the status the same process returns after its warm.
