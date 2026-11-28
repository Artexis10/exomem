## Why

A media job whose file kills the media process is retried forever. The supervisor restarts a process that dies by a signal after a delay that doubles to five minutes, and it then returns every job that the dead process was running to the queue. Nothing counts those deaths against the job. A file that crashes a native decoder each time it is read therefore crashes every later process too.

A measurement on 2026-11-27, on an installed personal service, showed the cost. Overnight, the media process died about 40 times on each of 8 videos. Each new process then started media offload again, and each offload took a census of every note in the vault under the vault's mutation boundary. Those holds lasted 39 s at the median, 104 s at p90 and 209 s at most, and saves and the epistemic graph waited behind them. That vault sets no media allowance, so each of those offloads was refused as unknown after its census.

## What Changes

- **A crash counts against the job.** When a media process dies without its own cleanup while it runs jobs, by a signal the supervisor did not send or by an exit status outside the media process protocol, each of those jobs counts one crash stop. The stop is recorded through the same claimed-job path as memory and watchdog stops: it refunds the attempt, and a speech job that made progress since its last stop counts nothing. A stop that the supervisor or the service manager asked for counts nothing. Preview jobs keep their own attempt ceiling.
- **The job is blocked after the stop limit.** After `EXOMEM_MEDIA_STOP_LIMIT` crash stops (three by default), the job is blocked with the new reason `child_crashed`. Its context names the signal or exit status and the release that ran it. The error names the same facts, and status tells the owner that a new release retries the file by itself, or that a retry tries it sooner. The other jobs keep running. The existing restart back-off is unchanged.
- **The job returns on a retry or under a new release.** An explicit retry returns it with no crash stops, and so does the first start of the media supervisor under another release. A new release may carry the fix for the decoder that crashed. A re-requested extraction also clears the count, as it clears the other stop counts.
- **No allowance, no census.** Offload takes no census when the media store policy sets no allowance. Without an allowance every offload is refused as `MEDIA_ALLOWANCE_UNKNOWN` whatever the census counts, a reused version included. The offload, the offload scan and the refusal status now reach that refusal without the census. This does not change a requirement: every decision stays the same.

## Capabilities

### Modified Capabilities

- `multimodal-job-runtime`: a job whose media process keeps dying is blocked after the stop limit, and returns on a retry or under another release.

## Impact

- Code: `src/exomem/media_jobs.py` (the `child_crashed` reason, the `crash_stops` column, the crash stop and the release return), `src/exomem/media_worker.py` (crash stops before interrupted jobs are recovered, the release return at supervisor start), `src/exomem/media_store.py`, `src/exomem/media_allowance.py` and `src/exomem/import_windows.py` (no census without an allowance).
- Data: the job ledger gains a `crash_stops` column, added in place with a default of 0. A ledger from before this change keeps every job; its running jobs start with no crash stops.
- Behaviour, crash stops: a crash now refunds the job's attempt, where before the job kept the attempt it had claimed. A job that crashed the process before this change starts its count at 0.
- Behaviour, no allowance: an offload refused for an unknown allowance no longer probes reuse candidates in the store, so it records no finding about them. The scheduled replica check still finds a damaged version. The refusal status names no notes behind an unknown census when no allowance is set, because the missing allowance is the whole reason.
