## Why

A media job whose file kills the media process is retried forever. The supervisor restarts a process that dies by a signal after a delay that doubles to five minutes, and it then returns every job that the dead process was running to the queue. Nothing counts those deaths against the job. A file that crashes the media process each time it is read therefore crashes every later process too.

A measurement on 2026-11-27, on an installed personal service, showed the cost. Overnight, the media process died about 40 times on each of 8 videos. The signal or exit status of those deaths is not known. Each new process then started media offload again, and each offload took a census of every note in the vault under the vault's mutation boundary. Those holds lasted 39 s at the median, 104 s at p90 and 209 s at most, and saves and the epistemic graph waited behind them. That vault sets no media allowance, so each of those offloads was refused as unknown after its census.

## What Changes

- **A job crash counts against the job.** A job crash is a death of the media process while it still holds its runtime record, so it ran none of its own cleanup. Its cause is a signal other than SIGTERM, SIGINT and SIGHUP, or an exit status other than 0 and the protocol statuses 75 to 81.
  - A death that the supervisor caused by its own stop is not a job crash. Neither is a PDF abort that is already counted as a memory stop.
  - Each job that the process ran counts one job crash, through the same claimed-job path as memory and watchdog stops. The attempt is refunded, and a speech job that made progress since its last stop counts nothing.
  - Preview jobs count none and keep their own attempt ceiling.
  - SIGKILL counts, so a file that the kernel's out-of-memory killer ends each time is blocked too.
  - A death that also ends the supervisor counts nothing: the next media process returns the job as before, with no bound on such restarts.
- **The job is blocked at the stop limit.** Job crashes add up over the job's life under one release, not only in a row, and one under another release starts the count again. At `EXOMEM_MEDIA_STOP_LIMIT` job crashes (three by default), the job is blocked with the new reason `child_crashed`. Its context names the signal or exit status and the release that ran it. The error names the same facts, and status tells the owner that a new release retries the file by itself, or that a retry tries it sooner. The other jobs keep running. The existing restart back-off is unchanged.
- **The job returns on a retry or once under a new release.**
  - An explicit retry returns it with no job crashes, and also resets its memory, watchdog and return counts.
  - The first start of the media supervisor under another release returns it with one try left, because that release may carry the fix. Only its crash count changes. A file that still crashes the process then costs one death per release, not three.
  - A re-requested extraction also resets the count, as it resets the other stop counts.
- **No allowance, no census.** Offload takes no census when the media store policy sets no allowance. Without an allowance every offload is refused as `MEDIA_ALLOWANCE_UNKNOWN` whatever the census counts, a reused version included. The offload, the offload scan and the refusal status now reach that refusal without the census. This does not change a requirement: every decision stays the same.

## Capabilities

### Modified Capabilities

- `multimodal-job-runtime`: a job crash counts against each job that the media process ran, and a job that keeps crashing the process is blocked at the stop limit until a retry or another release.

## Impact

- Code: `src/exomem/media_jobs.py` (the `child_crashed` reason, the `crash_stops` and `crash_release` columns, the job crash and the release return), `src/exomem/media_worker.py` (job crashes before interrupted jobs are recovered, the release return at supervisor start), `src/exomem/media_store.py`, `src/exomem/media_allowance.py` and `src/exomem/import_windows.py` (no census without an allowance).
- Data: the job ledger gains a `crash_stops` column with a default of 0 and a `crash_release` column with no default, both added in place. A ledger from before this change keeps every job; its jobs start with no job crashes.
- Behaviour, job crashes: a crash now refunds the job's attempt, where before the job kept the attempt it had claimed. A job that crashed the process before this change starts its count at 0.
- Behaviour, no allowance: an offload refused for an unknown allowance no longer probes reuse candidates in the store, so it records no finding about them. The `original-replica-custody` spec names that probe as the only finder of a lost version of an evicted filing. So in a vault with evicted filings and no valid allowance, such a loss stays unrecorded until an allowance is set. The refusal status names no notes behind an unknown census when no allowance is set, because the missing allowance is the whole reason.
