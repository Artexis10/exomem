# Derived import write probe

A dev-only instrument. It measures how many bytes a derived collection import writes
to disk per row, and what a stopped import costs. It is not shipped and not part of CI.

The probe creates a derived collection, imports a generated export through the public
import tool, and samples this process's `write_bytes` from `/proc/self/io` while the
import runs. It runs on Linux only.

## Shapes

`DERIVED_PROBE_SHAPE` selects the natural key:

| Shape | Natural key | Key order on arrival |
| --- | --- | --- |
| `date-series-index` | local date, a constant series, the sample index | ascending |
| `series-floating` | a constant series, a floating local date-time | ascending |
| `session-index` | a random text session per member, the sample index | ascending within a member |

## Run it

1. Check memory first: `harness resources admit --allowance 3`.
2. Run one repeat from the repository root:
   `DERIVED_PROBE_OUT=$SCRATCH/run.jsonl DERIVED_PROBE_SHAPE=date-series-index PYTHONPATH=tests uv run pytest -p conftest benchmarks/derived_import_writes/probe.py -q -p no:cacheprovider --timeout 3600`
3. Summarise the run: `python benchmarks/derived_import_writes/summarise.py $SCRATCH/run.jsonl`.

The default size is 160 members of 2,880 rows (460,800 rows). Set
`DERIVED_PROBE_MEMBERS` and `DERIVED_PROBE_ROWS` to change it.

Set `DERIVED_PROBE_CANCEL_ROWS=3000` to add the cancel arm. After the main import, a
second import starts on two larger members and is cancelled once it has staged that many
rows. The summary reports the bytes and seconds from the cancel to a ready collection,
and whether the collection's digest after the stop equals its digest before the job.

`DERIVED_PROBE_PRAGMAS` runs an ablation: for example
`cache_size=-65536;wal_autocheckpoint=16384` adds those statements to the derived
connection.

## Read the result

- `bytes_per_row`: bytes written over the whole import, divided by the rows imported.
- `window_bytes_per_row`: the same in eight windows. A flat series means the cost does
  not grow with the collection.
- `last_over_first_full`: the last window over the first full window. The first window
  also holds setup writes.
- `cancel.bytes` and `cancel.seconds` should scale with `cancel.staged_rows`, not with
  the collection's size, and `cancel.digest_equal` should be `true`.

Compare a change with its base on the same host, with the same shape and size, and run
at least two repeats of each.
