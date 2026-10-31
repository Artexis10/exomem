## Why

Each own-note `apply` call makes its first note alone to price a batch of notes. A note commit carries fixed work: the index, the log, the cited Source and a derived batch. A continuation repeats that solo commit even though the call before it has already timed notes on the same run. On a large import, every call after the first spends part of its budget on a price that the run already knows.

## What Changes

- The run records the dearest time per made note of the last call that made a note. The record is internal and does not appear in the run that a caller reads.
- A call's first batch holds as many notes as fit in its time at the recorded price, and at least one note. A run with no recorded price still makes its first note alone.
- A refused note still does not lower the price. A refused first batch makes its notes alone, and each note after the first still checks that it fits.

## Impact

- Affected specs: `adoption-studio`.
- Affected code: `src/exomem/adoption_run.py`.
