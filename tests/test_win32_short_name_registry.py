"""Real end-to-end proof of the Windows 8.3 short-name registry fix (#126).

The unit tests in `test_normalize_wikilink.py` / `test_inbound_index_incremental.py`
/ `test_file_watcher.py` pin the defense-in-depth and dispatch-guard layers
platform-free (they model the dual-form collapse via monkeypatching, so they run
on CI's Linux runners). This module is the on-box proof that REAL Windows 8.3
short-name aliasing (`REAL-V~1.MD`) triggers the exact failure #126 reported and
that the ingress canonicalization fix (`freshness._canonicalize_event_path`)
closes it.

GATED: skips unless running on Windows AND 8.3 short-name generation is enabled
for the volume backing the temp directory (`fsutil 8dot3name query <drive>`). A
long enough basename normally earns a short alias by default, but some volumes
have 8.3 generation disabled — skip gracefully rather than false-fail there.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows 8.3 short-name aliasing only applies on Windows",
)


def _short_path_name(path: Path) -> str | None:
    """The Windows 8.3 short form of an EXISTING path, or None if unavailable
    (API missing, or 8.3 generation disabled for the volume)."""
    if sys.platform != "win32":
        return None
    get_short = ctypes.windll.kernel32.GetShortPathNameW  # type: ignore[attr-defined]
    get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    buf = ctypes.create_unicode_buffer(1024)
    n = get_short(str(path), buf, 1024)
    if n == 0:
        return None
    return buf.value


def test_reconcile_heals_resolver_after_8dot3_registry_drift(vault: Path) -> None:
    """The exact production mechanism behind #126, reproduced with a REAL
    Windows 8.3 short-name alias:

    1. A note with a long (>8.3) basename is seeded live in both the freshness
       registry and the process-shared WikilinkResolver, keyed under its long
       form (a correct initial seed).
    2. The registry then drifts to holding the SAME file under its 8.3 SHORT
       form (modeling a watchdog event that reported `event.src_path` in short
       form — the exact split the module docstring in `freshness.py` describes).
    3. The 300s periodic reconcile fires (`FileWatcher._reconcile_once`): its
       fresh walk always yields the long form, so it detects "1 changed
       (long), 1 deleted (short)" and dispatches that delta through the event
       fan-out — the SAME path #126's writers hit.

    Before the fix, the resolver lost the note's entry after step 3 even
    though the file was untouched on disk the whole time (the false "does not
    resolve to any file in the vault" writer warning). After the fix, the
    entry survives.
    """
    from exomem import find as find_module
    from exomem import freshness
    from exomem.file_watcher import FileWatcher

    notes = vault / "Knowledge Base" / "Notes" / "Failures"
    notes.mkdir(parents=True, exist_ok=True)
    # A ~96-char slug — long enough to earn an 8.3 short alias (the exact
    # length class #126 reported), short enough to fit comfortably in
    # MAX_PATH for the test's tmp_path-backed vault.
    name = (
        "real-vault-find-thrash-lexical-sidecar-heal-is-a-full-o-corpus-"
        "rebuild-triggered-constantly-by-drift"
    )
    abspath = notes / (name + ".md")
    abspath.write_text('---\ntitle: "Dual Form"\n---\n\nbody\n', encoding="utf-8")
    mt = abspath.stat().st_mtime_ns

    short_form = _short_path_name(abspath)
    if short_form is None or short_form == str(abspath):
        pytest.skip(
            "8.3 short-name generation is disabled for this volume "
            "(fsutil 8dot3name) — nothing to reproduce here"
        )

    rel = abspath.relative_to(vault).with_suffix("").as_posix()

    try:
        # 1. Correct initial seed: registry + shared resolver both keyed
        #    under the file's long form.
        freshness.invalidate(vault)
        freshness.seed(vault, "vault", [(str(abspath), mt)])
        freshness.seed(vault, "kb", [(str(abspath), mt)])
        resolver = find_module.shared_resolver(vault)
        assert rel in resolver.full_paths, "test setup: resolver should see the seeded note"

        # 2. Registry drifts to the SAME file's 8.3 SHORT form (the watchdog
        #    split #126's mechanism depends on).
        freshness.invalidate(vault)
        freshness.seed(vault, "vault", [(short_form, mt)])
        freshness.seed(vault, "kb", [(short_form, mt)])

        # 3. The 300s periodic reconcile: fresh walk (long form) vs. the
        #    drifted map (short form) -> dispatches the drift delta through
        #    the same event fan-out a live batch uses.
        FileWatcher(vault)._reconcile_once(seed=False)

        assert abspath.is_file(), "test invariant: the file was never touched on disk"
        assert rel in resolver.full_paths, (
            "the shared WikilinkResolver dropped a live, on-disk note after a "
            "reconcile healed a Windows 8.3 short-name registry drift — the "
            "exact false 'does not resolve to any file in the vault' warning #126 "
            "reported"
        )
    finally:
        freshness.invalidate(vault)
        find_module._RESOLVER_CACHE.pop(vault, None)


def test_on_files_changed_canonicalizes_8dot3_short_form_to_long_form_key(
    vault: Path,
) -> None:
    """Isolates the root fix: `freshness.on_files_changed` fed a REAL 8.3
    short-form event path (as a watchdog `on_modified` callback would report
    it) must key the registry map under the file's long form — not the raw
    short form — so it lines up with the walk side's keys."""
    from exomem import freshness

    notes = vault / "Knowledge Base" / "Notes" / "Failures"
    notes.mkdir(parents=True, exist_ok=True)
    name = (
        "real-vault-find-thrash-lexical-sidecar-heal-is-a-full-o-corpus-"
        "rebuild-triggered-constantly-by-drift-ingress"
    )
    abspath = notes / (name + ".md")
    abspath.write_text("body\n", encoding="utf-8")

    short_form = _short_path_name(abspath)
    if short_form is None or short_form == str(abspath):
        pytest.skip(
            "8.3 short-name generation is disabled for this volume "
            "(fsutil 8dot3name) — nothing to reproduce here"
        )

    try:
        freshness.invalidate(vault)
        freshness.seed(vault, "vault", [])
        freshness.seed(vault, "kb", [])

        freshness.on_files_changed(vault, changed=[Path(short_form)], deleted=[])

        live = freshness.live_entries(vault, "vault")
        assert live is not None
        assert str(abspath) in live, (
            f"ingress did not canonicalize the 8.3 short form to the long-form "
            f"key; live keys: {list(live.keys())}"
        )
        assert short_form not in live, "ingress left the raw short form as a live key"
    finally:
        freshness.invalidate(vault)
