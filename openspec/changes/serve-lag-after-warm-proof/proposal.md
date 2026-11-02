## Why

During an online upgrade under writes, the promoted worker proves its recall
catalogue strictly and reports ready about 26 seconds after the switch. Its warm
then continues with the graph handoff, the semantic corpus and optional model
preloads. Writes keep landing in that time, and each one moves the live
projection ahead of the catalogue for a few seconds, until the lexical apply
lands.

Today the next readiness probe revokes strict admission, and a revoked catalogue
reads as `warming` while the warm runs. The served-lag rule holds only once the
whole warm has finished, and the probe may not re-prove before the required warm
stages finish. `/health/ready` therefore answered 503 for about 75 seconds right
after the worker first went ready, and recall refused with its warming outcome
over the same period. The worker had already proved the catalogue it would
serve.

## What Changes

- A managed process that has proved both catalogues exactly equal to the live
  projections since its warm began treats a later strict failure as it does
  after the warm, while the rest of the warm still runs.
- Health reports such a lag as `lagging` with status ready, under the existing
  served-lag conditions: event indexes enabled, both live projections and both
  published checkpoints present, and the access-policy and semantic-catalogue
  identity unchanged. `admitted` stays false and no reason code is added.
- Recall serves the published catalogue with the projection lag disclosed
  instead of refusing with its warming outcome. Lanes that are not ready are
  still skipped and named in the `warming` object.
- Each probe while lagging proves again, and a strict proof that holds re-admits
  retrieval during the warm. A lagging answer is still never reused from the
  ready-proof cache.
- A process that has not proved equality since its warm began keeps today's
  behaviour: health reports warming and recall refuses with its warming outcome
  while that warm runs.

## Capabilities

### Modified Capabilities

- `install-readiness`: "Runtime Health Distinguishes Transport And Recall
  Admission" serves the lag from the warm's strict proof on. The scenario "A
  running warm withholds the served lag" keeps its name and now covers only a
  warm that has not proved its catalogue. A scenario is added for a warm that
  has.
- `recall-read-path`: "Published catalogue admission survives strict readiness
  demotion" applies during the warm once this process has made its strict proof.

The active change `admit-retrieval-after-required-warm` has a scenario that
waits for the required warm stages before any re-proof. It now names the process
that has not yet proved its catalogue in this warm. The active change
`keep-recall-available-during-pending-recovery` modifies the same recall-read-path
requirement, and its first sentence is amended in step.

`runtime-readiness-contract` is unchanged: a served lag still leaves takeover
eligibility and reason codes unchanged.

## Impact

- `src/exomem/readiness.py`: a proven process whose ready bit is clear reports
  `unavailable` instead of `warming`, and may re-prove and re-admit during the
  warm.
- `src/exomem/runtime_readiness.py`: documentation only. The served-lag check
  already follows the `unavailable` state and the proof flag.
- `src/exomem/find.py`: no change. Its warm gate refuses only the `warming`
  state, so it serves the proven case through the shared state.
- Behaviour: a promoted or restarted worker under writes stays ready from its
  first catalogue proof. A proven process whose published catalogue cannot be
  served during the warm refuses recall with the retryable
  `temporarily_unavailable` status, as after the warm, instead of `warming`.
