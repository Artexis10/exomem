"""Keep the converted contention suites free of tight wall-clock literals.

Four separate CI failures across two days were one defect: a wall-clock number
that exists so a hang fails instead of blocking forever, written tightly enough
that it also claims a contended runner is fast.

Fixing them was not the hard part. Keeping them fixed was -- #689 converted the
tests failing that day and left the rest, and the leftovers took out a Windows
shard four days later. Then the follow-up sweep matched `.wait(2)` but not
`.wait(2.0)` and the decimal survivors failed on the very next run.

So the files that have been converted are pinned here. This deliberately does
NOT police the whole suite: roughly 250 literal waits exist across 35 files and
most are not this defect -- an event set synchronously never needs a generous
window. It guards the files that have actually cost CI time, and it is cheap to
add another to the tuple once it has been converted.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

TESTS = Path(__file__).parent

#: Files whose contention waits have been converted to named hold/observe
#: constants. Adding a file here is a promise that it contains no tight
#: positive wait, so convert it first.
CONVERTED = (
    "test_command_surface_retry.py",
    "test_corpus_context_cache.py",
    "test_lexstore_atomic_rebuild.py",
    "test_graph_rebuild_locking.py",
    "test_mutation_concurrency.py",
    "test_graph_rebuild_availability.py",
    "test_mutation_lock.py",
    "test_epistemic_graph_freshness.py",
    "test_writer_lease.py",
    "test_continuation_checkpoint.py",
)

#: `assert not <event>.wait(0.05)` is a NEGATIVE observation -- it proves
#: something has not happened YET -- and it must stay tight. Widening one does
#: not just slow the test, it changes the scenario, because the product's own
#: timeouts run during the same window. A slow runner can only make these pass
#: vacuously, never fail, which is why they are safe where a positive wait is
#: not. They are exempt by construction, not by allowlist.
_NEGATIVE = re.compile(r"\bnot\s+[A-Za-z_][A-Za-z0-9_]*\.wait\(")

#: Both the positional and the keyword spelling. An earlier sweep matched only
#: `.wait(2)` and missed every `.wait(2.0)`; a later one matched only the bare
#: form and missed every `.wait(timeout=2)`. Each gap shipped and each one came
#: back as a CI failure, so the detector accepts all four shapes.
_POSITIVE_LITERAL_WAIT = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\.wait\((?:timeout=)?(\d+(?:\.\d+)?)\)"
)
_LITERAL_JOIN = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\.join\((?:timeout=)?(\d+(?:\.\d+)?)\)"
)


#: `t.join(timeout=0.3)` followed by `assert t.is_alive()` is the same negative
#: observation in join form: it proves a competitor is STILL parked. It is also
#: the only shape that spends its entire window on a healthy run -- every other
#: wait here returns the moment its event fires. Widening one is therefore pure
#: cost, and the first sweep widened this exact site from 0.3s to 60s, where it
#: raced pytest's own 60s per-test timeout and took out a Linux shard with a
#: thread dump instead of an assertion.
_STILL_PARKED = re.compile(r"assert\s+(?!not\b)([A-Za-z_][A-Za-z0-9_]*)\.is_alive\(\)")


def _still_parked_after(lines: list[str], index: int, thread: str) -> bool:
    """True when a join is followed by `assert <thread>.is_alive()`.

    `assert not <thread>.is_alive()` is the opposite claim -- it waits for
    completion -- so it is deliberately not matched here and stays governed by
    the literal rule.
    """
    for nearby in lines[index + 1 : index + 4]:
        match = _STILL_PARKED.search(nearby)
        if match and match.group(1) == thread:
            return True
    return False


def _offenders(source: str) -> list[tuple[int, str]]:
    lines = source.splitlines()
    found: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _NEGATIVE.search(line):
            continue
        join = _LITERAL_JOIN.search(line)
        if join and _still_parked_after(lines, index, join.group(1)):
            continue
        if _POSITIVE_LITERAL_WAIT.search(line) or join:
            found.append((index + 1, stripped))
    return found


@pytest.mark.parametrize("name", CONVERTED)
def test_converted_suite_has_no_tight_positive_wait(name: str) -> None:
    path = TESTS / name
    assert path.is_file(), f"{name} is listed as converted but does not exist"

    offenders = _offenders(path.read_text(encoding="utf-8"))

    assert not offenders, (
        f"{name} has regained literal wall-clock waits. Use the file's named "
        "hold/observe constants instead.\n\n"
        "A HOLD parks a lock or an in-flight rebuild while the test observes an "
        "ordering, and must outlast the observation or the ordering passes "
        "vacuously. An OBSERVATION is how long the test waits for a state to be "
        "reached, and must be generous -- the hold above is what keeps it "
        "discriminating, so a wrong ordering still fails while a loaded shard "
        "does not.\n\n"
        "A negative wait (`assert not x.wait(0.05)`) is exempt and should stay "
        "tight.\n\n"
        + "\n".join(f"  line {number}: {text}" for number, text in offenders)
    )


def test_the_guard_can_actually_fail() -> None:
    """A hygiene check that cannot go red is worse than none.

    The first version of the sibling Codex-lane guard matched a string that also
    appeared in the explanatory comment beside it, so it could never fail. This
    proves the detector fires on a positive literal, ignores comments, and
    exempts the negative form.
    """
    assert _offenders("    assert entered.wait(1.0)\n")
    assert _offenders("    thread.join(timeout=3)\n")
    assert not _offenders("    # assert entered.wait(1.0)\n")
    assert not _offenders("    assert not holder_entered.wait(0.05)\n")
    assert not _offenders("    assert entered.wait(_OBSERVE_SECONDS)\n")
    assert not _offenders("    worker.join(0.3)\n    assert worker.is_alive()\n")
    assert _offenders("    worker.join(0.3)\n    assert not worker.is_alive()\n")
    assert _offenders("    worker.join(0.3)\n    assert other.is_alive()\n")


def _per_test_timeout_seconds() -> float:
    config = tomllib.loads((TESTS.parent / "pyproject.toml").read_text(encoding="utf-8"))
    return float(config["tool"]["pytest"]["ini_options"]["timeout"])


_SECONDS_CONSTANT = re.compile(
    r"^(_[A-Z][A-Z0-9_]*SECONDS)\s*=\s*(\d+(?:\.\d+)?)\s*$", re.MULTILINE
)


def test_timing_constants_stay_under_pytests_own_timeout() -> None:
    """A valve at or above the harness timeout never gets to fire.

    Ten of these constants were written as exactly 60.0 -- the same number as
    `timeout` in pyproject. A valve is supposed to convert a hang into a named
    assertion; at parity with the harness the harness wins the race, and the
    run reports a thread dump and no junit XML instead. That is how a one-line
    ordering bug arrived as an unreadable Linux shard failure.

    Scoped to test modules. `tests/conftest.py` holds a longer quiesce valve
    that runs in teardown, a different phase with its own budget, and there is
    no evidence it is mis-sized.
    """
    limit = _per_test_timeout_seconds()
    over: list[str] = []
    for path in sorted(TESTS.glob("test_*.py")):
        for name, value in _SECONDS_CONSTANT.findall(path.read_text(encoding="utf-8")):
            if float(value) >= limit:
                over.append(f"  {path.name}: {name} = {value}")

    assert not over, (
        f"These wall-clock constants are at or above pytest's per-test timeout "
        f"({limit}s), so they can never report a failure of their own -- the "
        "harness kills the test first.\n\n" + "\n".join(over)
    )
