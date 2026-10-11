# Proposal: disclose-unsupported-platform

## Why

`6587ad8c fix(governance): reserve internal state paths` added
`src/exomem/reserved_paths.py`, which acquires a root through `held_fs` before
every governed write. `_held_fs_posix._probe` opens with
`if not sys.platform.startswith("linux")` and returns disabled capabilities, so
darwin has no backend at all — and `acquire` is documented to refuse rather than
fall back: "no failed probe gets a fallback."

The result was not a refusal a user could act on. On the run at that commit all
four macOS shards failed while all four Windows shards passed, and a shard
reports roughly `1035 failed, 2160 passed`. The failures read
`reserved identity catalogue could not acquire the vault`,
`private SQLite target cannot acquire the vault`, and
`PATH_GUARD_UNSAFE: held filesystem route is unavailable` — a permanent fact
about the build, phrased each time as though this particular filesystem were at
fault. Behind them sits a `GRAPH_SYNC_LINEAGE_CONFLICT` cascade, because writes
that cannot publish leave the graph epoch genuinely incoherent.

The commit before it changed only `scripts/deploy.ps1` and had three of four
macOS shards passing, which is what makes the boundary legible.

Meanwhile the package advertised `Operating System :: OS Independent`,
`install-readiness` required that "macOS Apple Silicon SHALL default to native
setup for MPS/MLX support", and `docs/deployment.md` drew macOS into the host
diagram and shipped a launchd service recipe for it. Every one of those claims a
platform where the write path refuses.

This change makes the gap honest. It does not close it: there is still no darwin
backend, and nothing here pretends otherwise.

## What Changes

- `held_fs` gains `platform_support()`, a root-free question — *does this build
  have a backend at all* — distinct from `probe`, which asks whether one
  filesystem can support the primitives. `_probe` and the new predicate share
  one platform rule rather than keeping two copies.
- A vault-touching CLI command on a host with no backend refuses once, naming
  the platform and the served ones, and points at the doctor. `doctor` and
  `install-info` stay reachable, because a user has to be able to ask what is
  wrong.
- `doctor` reports `platform.held_filesystem` as a **failure**, not a warning:
  the vault is not degraded there, it is unusable.
- The package stops claiming `Operating System :: OS Independent` and names
  Linux and Windows.
- The test suite reports the substrate's own refusals as skips through the
  existing `pytest_runtest_makereport` hook, which already does exactly this for
  five other absent platform capabilities.
- README and `docs/deployment.md` say macOS cannot be served. The launchd recipe
  is kept and labelled rather than deleted, so the intended shape survives for
  whoever writes the backend.

## Capabilities

### New Capabilities

- none

### Modified Capabilities

- `install-readiness` — adds the refusal-and-disclosure requirement, and
  corrects the live requirement that recommends a native macOS runtime.

## Impact

- `src/exomem/held_fs.py`, `src/exomem/_held_fs_posix.py`,
  `src/exomem/_held_fs_windows.py` — the root-free predicate.
- `src/exomem/__main__.py` — the single refusal and its allowlist.
- `src/exomem/doctor.py` — `platform.held_filesystem`.
- `pyproject.toml` — classifiers.
- `tests/benchmark_capabilities.py`, `tests/conftest.py` — one more branch in an
  existing hook.
- `README.md`, `docs/deployment.md`.
- No tool-surface movement: no descriptor, docstring, or parameter changes.

## What this does not do

macOS still cannot run exomem. Porting the substrate is a separate change and
needs a macOS host to prove: darwin has `openat`, `renameat`, `unlinkat`,
`linkat` and `renameatx_np` with `RENAME_EXCL`, but no `renameat2`, no `O_PATH`
and no `/proc/self/fd`, so it is a second implementation rather than a relaxed
predicate.

The test-suite skip is also narrower than the failure. It matches only the
refusals that name the acquisition; the `GRAPH_SYNC_LINEAGE_CONFLICT` cascade
behind them is deliberately left failing, because that error is
indistinguishable from the real lineage defects this repository has had, and
matching a downstream symptom would mask them. How much of a macOS shard that
leaves red is not asserted here — it cannot be measured from a Linux host, and
this change does not claim a green macOS.
