"""Installed-product acceptance for fast durable write acknowledgement.

What this measures is the complete public write a person actually waits for --
entry to acknowledgement -- and the read that immediately follows it, on a real
installed cell, separately for the in-process server and for the connector in
front of it. The deterministic gate in ``semantic_write_latency.py`` proves the
shape on synthetic corpora; this proves it where the corpus, the hardware and
the transport are real.

Four things have to hold together, and each can fail the run on its own:

* default-write p50 at or below 3.0 s and p90 at or below 5.0 s, per operation;
* the read immediately after a write is exact, or explicitly warming -- a stale
  answer is never acceptable, because deferring derived work must not become
  losing it;
* no write covered by exact component custody mints an uncovered full-index
  receipt, which would mean the change had traded a bounded deferral for
  whole-vault debt;
* the backlog a burst creates converges within a bounded window rather than
  leaving recall warm indefinitely.

Everything this prints is content-free: closed codes, counts, percentiles and
ages. No path, title, excerpt, query term or exception text leaves the process,
because an acceptance artifact is read and pasted by people who are not the
vault's owner.

It runs against a disposable vault it is handed and nothing else. Resolving a
vault from the ambient environment is refused rather than discouraged: pointing
this at a live cell is the one way it could do harm.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import managed_recall  # noqa: E402

from exomem import (  # noqa: E402
    deferred_index,
    derived_drain,
    derived_receipts,
    memory_refs,
    pending_recall,
    readiness,
    semantic_writes,
    writer_lease,
)
from exomem import find as find_module  # noqa: E402
from exomem import vault as vault_module  # noqa: E402

#: The design's installed-product numbers. Not this script's to tune.
P50_MS = 3_000.0
P90_MS = 5_000.0
#: The acceptance requires at least this many samples of each operation per
#: transport. Fewer is not a faster run; it is a weaker claim.
MIN_SAMPLES_PER_OPERATION = 30
#: A burst has to drain, not merely be durable. Five minutes is the design's own
#: oldest-required-component alarm, so exceeding it here is the same breach the
#: rollout would pause on.
CONVERGENCE_BOUND_SECONDS = 300.0
OPERATIONS = ("remember", "edit")
TRANSPORTS = ("direct", "connector")

#: Bounded so one pathological response cannot be read into memory.
_MAX_HTTP_RESPONSE_BYTES = 1_048_576
_CONNECTOR_TIMEOUT_SECONDS = 30.0


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(
        len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.999) - 1)
    )
    return ordered[index]


def _page(title: str, marker: str, identity: str) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        # An entity page carrying observations is the shape the deterministic
        # gate already writes, and the one whose edit route does not demand a
        # relation-disposition handoff -- so this measures acknowledgement
        # latency rather than the cost of satisfying a review contract.
        "type: entity\n"
        "status: active\n"
        "updated: 2026-09-02\n"
        f"{memory_refs.ID_FIELD}: {identity}\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Observations\n\n"
        f"- [config] {marker} #acceptance (live) ^acceptance-anchor\n"
    )


class _DirectTransport:
    """The in-process server: the real lease manager over the real leaves."""

    name = "direct"

    def __init__(self, vault_root: Path, state_dir: Path) -> None:
        self.vault_root = vault_root
        self.manager = writer_lease.LeaseManager(
            writer_lease.LeaseConfig(state_dir=state_dir)
        )

    def write(self, rel_path: str, source: str) -> float:
        target = self.vault_root / rel_path

        def leaf(root: Path):
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                preflight = semantic_writes.preflight_existing(
                    root, path=rel_path, after_source=source, operation="observe"
                )
                if preflight.contract_result.should_block:
                    codes = [
                        item.code
                        for item in preflight.contract_result.blocking_findings
                    ]
                    raise RuntimeError(f"acceptance write was blocked: {codes}")
                semantic_writes.commit_existing(root, preflight=preflight)
            else:
                vault_module.batch_atomic_write(
                    [vault_module.PlannedWrite(target, source, create_only=True)],
                    vault_root=root,
                )
            return {"path": rel_path, "warnings": []}

        command = SimpleNamespace(name="remember", leaf=leaf, read_only=False)
        started = time.perf_counter()
        self.manager.invoke(
            command,
            (self.vault_root,),
            {"response_detail": "compact"},
            mutation_request_id=str(uuid.uuid4()),
        )
        return (time.perf_counter() - started) * 1_000.0


class _ConnectorTransport:
    """The connector in front of the cell, measured separately by design.

    A connector run that cannot reach a connector fails: silently measuring the
    in-process path under the connector's name would report the one number the
    acceptance exists to measure separately.
    """

    name = "connector"

    def __init__(self, url: str, token: str | None) -> None:
        self.url = url
        self.token = token

    def write(self, rel_path: str, source: str) -> float:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "tools/call",
                "params": {
                    "name": "remember",
                    "arguments": {"path": rel_path, "content": source},
                },
            }
        ).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        request = Request(self.url, data=payload, headers=headers, method="POST")
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=_CONNECTOR_TIMEOUT_SECONDS) as response:
                response.read(_MAX_HTTP_RESPONSE_BYTES)
        except (HTTPError, URLError, TimeoutError) as error:
            # The class only: a transport error's message can carry a host, a
            # path or a token fragment.
            raise SystemExit(
                f"live write acceptance failed: connector call failed "
                f"({type(error).__name__})"
            ) from error
        return (time.perf_counter() - started) * 1_000.0


def _read_your_write(vault_root: Path, marker: str, rel_path: str) -> str:
    """One closed outcome for the read that immediately follows a write."""
    try:
        hits = find_module.find(
            vault_root, query=marker, scope="kb-only", mode="keyword", limit=5
        )
    except find_module.RetrievalIndexWarming:
        # A truthful refusal. The contract permits it; it is never a stale
        # answer, which is the outcome that matters.
        return "warming"
    except Exception:  # noqa: BLE001 - an unreadable answer is not an exact one
        return "stale"
    return "exact" if any(hit.path == rel_path for hit in hits) else "stale"


def _full_receipt_count(vault_root: Path) -> int:
    try:
        return len(deferred_index.list_full_paths(vault_root))
    except Exception:  # noqa: BLE001 - an unreadable counter cannot prove zero
        return -1


def _reconciliation_demanded(vault_root: Path) -> int:
    """Batches that fail closed into explicit reconciliation demand.

    This is deliberately NOT folded into convergence. A `reconcile_required`
    receipt is not a backlog that more draining would clear -- it is the
    store's declared, terminal statement that it could prove neither the
    complete before-state nor the intended after-state. A burst of ordinary
    writes producing one is a real acceptance failure, and reporting it as
    "still converging" would let it look like slowness.
    """
    return derived_receipts.recoverable_batch_count(vault_root)


def _drain_observation(vault_root: Path) -> dict[str, int]:
    """What the store says about components a burst has not settled yet.

    Content-free by construction: three counts read straight off the receipt
    store, no batch id, component name, path or failure text. `stuck` is the
    one that matters -- a component that has already been attempted and is
    either backing off into the future or carrying a failure code is not
    something more draining will clear, and reporting it as backlog is how a
    run that needs a person gets read as a run that needs more time.

    An unreadable store reports -1 rather than zero, because a counter that
    cannot be read has not proved anything.
    """
    try:
        due = int(derived_receipts.due_component_count(vault_root))
    except Exception:  # noqa: BLE001 - an unreadable counter cannot prove zero
        due = -1

    stuck = 0
    failed = 0
    max_attempt_count = 0
    rows: list[tuple[Any, Any, Any]] = []
    try:
        if deferred_index.store_path(vault_root).exists():
            connection = deferred_index._connect_readonly(vault_root)
            try:
                rows = connection.execute(
                    "SELECT c.attempt_count, c.next_attempt_at, c.failure_code "
                    "FROM derived_batch_components AS c "
                    "JOIN derived_batches AS b ON b.batch_id = c.batch_id "
                    "WHERE b.state = 'ready' "
                    "AND c.state IN ('ready', 'claimed', 'retryable')"
                ).fetchall()
            finally:
                connection.close()
    except Exception:  # noqa: BLE001 - same
        return {"due": due, "stuck": -1, "failed": -1, "max_attempt_count": -1}

    now = time.time()
    for attempt_count, next_attempt_at, failure_code in rows:
        attempts = int(attempt_count or 0)
        max_attempt_count = max(max_attempt_count, attempts)
        if failure_code is not None:
            failed += 1
        if attempts > 0 and (
            failure_code is not None or float(next_attempt_at or 0.0) > now
        ):
            stuck += 1
    return {
        "due": due,
        "stuck": stuck,
        "failed": failed,
        "max_attempt_count": max_attempt_count,
    }


def _converge(
    vault_root: Path, *, bound_seconds: float = CONVERGENCE_BOUND_SECONDS
) -> tuple[float, bool, dict[str, Any]]:
    """Drain the burst in bounded passes and report what the drain did.

    The wall clock is the point: the store's retry backoff is a wall-clock
    deadline, so a burst's real convergence window is a real wait. Between
    passes it yields rather than spinning, because a hot loop competes with the
    very workers it is waiting on.

    A pass claims `derived_drain.progress_limit()` -- the scheduler's own
    allowance for this mode -- rather than a number this script invented. The
    invented one was 32, which is the performance-mode ceiling and not the
    default in any mode this runs in; on a real corpus one component dispatch
    takes 18-88 seconds, so a pass of 32 is a block of up to three quarters of
    an hour inside which no deadline exists. Measured: 99 seconds of drain
    against a 30-second bound, and 315 against the design's 300. The bound was
    being reported, not enforced.

    What the drain saw is reported alongside whether it finished, because
    "did not converge" cannot distinguish the two outcomes an operator acts on
    differently -- a deep backlog that is draining and wants a longer window,
    and a drain that is stuck and wants a person.
    """
    dispatch = derived_drain.component_dispatcher()
    observe = derived_drain.canonical_generation_observer()
    started = time.monotonic()
    deadline = started + float(bound_seconds)
    passes = 0
    completed = 0
    stalled_passes = 0
    while True:
        passes += 1
        moved = derived_drain.drain_once(
            vault_root,
            dispatch=dispatch,
            observe_current_generation=observe,
            visibility_publisher=pending_recall.publish,
            limit=derived_drain.progress_limit(),
        )
        completed += int(moved)
        stalled_passes = 0 if moved else stalled_passes + 1
        elapsed = time.monotonic() - started
        observation = _drain_observation(vault_root)
        drain = {
            "passes": passes,
            "completed": completed,
            "failed": observation["failed"],
            "stuck": observation["stuck"],
            "stalled_passes": stalled_passes,
            "rate_components_per_min": (
                round(completed / elapsed * 60.0, 2) if elapsed > 0 else 0.0
            ),
            "projected_seconds_to_converge": None,
        }
        due = observation["due"]
        if due > 0 and completed > 0 and elapsed > 0:
            drain["projected_seconds_to_converge"] = round(
                due * elapsed / completed, 1
            )
        if due == 0:
            return round(elapsed, 3), True, drain
        # Each of these is a reported failure with its own name, and the loop
        # stops at the first one: a stuck or failed claim will not clear by
        # waiting, two passes that moved nothing are not draining, and a
        # crossed deadline is the answer the bound exists to give.
        if observation["stuck"] or observation["failed"]:
            return round(elapsed, 3), False, drain
        if stalled_passes >= 2:
            return round(elapsed, 3), False, drain
        if time.monotonic() >= deadline:
            return round(elapsed, 3), False, drain
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def measure(
    vault_root: Path,
    *,
    transport: str,
    samples_per_operation: int,
    state_dir: Path,
    connector_url: str | None = None,
    connector_token: str | None = None,
    convergence_bound_seconds: float = CONVERGENCE_BOUND_SECONDS,
) -> dict[str, Any]:
    """Run one transport's acceptance burst and return a content-free report."""
    root = Path(vault_root)
    if transport == "connector":
        if not connector_url:
            raise SystemExit(
                "live write acceptance failed: connector transport requires a "
                "--connector-url or EXOMEM_CONNECTOR_URL"
            )
        client: Any = _ConnectorTransport(connector_url, connector_token)
    else:
        state_dir.mkdir(parents=True, exist_ok=True)
        client = _DirectTransport(root, state_dir)

    os.environ["EXOMEM_FAST_DURABLE_ACK"] = "1"
    # Managed admission, because the pending overlay is scoped to managed
    # recall and an offline caller keeps its source-walk fallback -- and
    # asserted, not merely recorded. `warming` is a truthful outcome of the
    # read contract, but a harness that never had an admitted catalogue answers
    # `warming` to everything and measures an unwarmed corpus-context build on
    # every write. Measured on a 3,789-page copy, that was the difference
    # between a 377ms edit median with exact reads and a 2,623ms one with a
    # 27.7s first sample and no exact read at all. The acceptance would have
    # been reporting the harness.
    #
    # This is the served process's own warm-up, shared with the deterministic
    # gate as one object: see `scripts/managed_recall.py`.
    durations: dict[str, list[float]] = {name: [] for name in OPERATIONS}
    outcomes = {"exact": 0, "warming": 0, "stale": 0}
    # The warm-up manages the runtime, so it belongs inside the same `finally`
    # that gives it back: a warm-up that raises after managing would otherwise
    # leave this process managed for good.
    try:
        recall_admission = str(
            managed_recall.enter_managed_recall(root).get("state", "unknown")
        )
        full_receipts_before = _full_receipt_count(root)
        for index in range(samples_per_operation):
            identity = str(uuid.UUID(int=index + 1))
            # An entity page belongs under Entities/: the semantic contract
            # pairs a declared type with its location, and a benchmark that
            # fought that contract would be measuring the refusal.
            rel_path = (
                f"Knowledge Base/Entities/Concepts/acceptance-{index:04d}.md"
            )
            create_marker = f"acceptanceremember{index:04d}"
            durations["remember"].append(
                client.write(
                    rel_path,
                    _page(f"Acceptance {index:04d}", create_marker, identity),
                )
            )
            outcomes[_read_your_write(root, create_marker, rel_path)] += 1

            edit_marker = f"acceptanceedit{index:04d}"
            durations["edit"].append(
                client.write(
                    rel_path,
                    _page(f"Acceptance {index:04d}", edit_marker, identity),
                )
            )
            outcomes[_read_your_write(root, edit_marker, rel_path)] += 1
    finally:
        readiness.unmanage_runtime()

    convergence_seconds, converged, drain = _converge(
        root, bound_seconds=convergence_bound_seconds
    )
    full_receipts_after = _full_receipt_count(root)
    uncovered = (
        -1
        if full_receipts_before < 0 or full_receipts_after < 0
        else max(0, full_receipts_after - full_receipts_before)
    )
    return {
        "transport": transport,
        "samples_per_operation": samples_per_operation,
        "fast_durable_ack": (
            "active" if writer_lease.fast_durable_ack_active() else "inactive"
        ),
        "operations": {
            name: {
                "samples": len(values),
                "p50_ms": round(statistics.median(values), 1),
                "p90_ms": round(_percentile(values, 0.90), 1),
                "max_ms": round(max(values), 1),
            }
            for name, values in durations.items()
        },
        "recall_admission": recall_admission,
        "read_your_write": outcomes,
        "uncovered_full_receipts": uncovered,
        "post_burst_convergence_seconds": convergence_seconds,
        "post_burst_convergence_bound_seconds": round(
            float(convergence_bound_seconds), 1
        ),
        "post_burst_converged": converged,
        "drain": drain,
        "reconciliation_demanded": _reconciliation_demanded(root),
    }


def _convergence_failure(report: dict[str, Any]) -> str:
    """Say which non-convergence this was, because they are not one thing.

    A deep backlog that is draining wants a longer window and a look at what a
    single dispatch costs. A stuck drain wants a person: a claim already
    attempted and backing off, or one carrying a failure code, will not clear
    by waiting. "Did not converge" is true of both and actionable for neither,
    and it is what the first live run said.
    """
    bound = float(
        report.get(
            "post_burst_convergence_bound_seconds", CONVERGENCE_BOUND_SECONDS
        )
    )
    headline = f"the post-burst backlog did not converge within {bound:.0f}s"
    drain = report.get("drain")
    if not isinstance(drain, dict):
        return headline
    passes = int(drain.get("passes", 0))
    stuck = int(drain.get("stuck", 0))
    failed = int(drain.get("failed", 0))
    if stuck < 0 or failed < 0:
        return f"{headline}: the drain observation was unreadable"
    if stuck or failed:
        return (
            f"{headline}: drain stuck ({stuck} component(s) held back, "
            f"{failed} failed) after {passes} pass(es)"
        )
    if int(drain.get("stalled_passes", 0)) >= 2:
        return (
            f"{headline}: drain stuck (no component completed in the last "
            f"2 of {passes} pass(es))"
        )
    rate = float(drain.get("rate_components_per_min") or 0.0)
    if rate <= 0.0:
        return (
            f"{headline}: drain stuck (no component completed in "
            f"{passes} pass(es))"
        )
    projected = drain.get("projected_seconds_to_converge")
    projection = (
        "" if projected is None else f", projected {float(projected):.0f}s to drain"
    )
    return (
        f"{headline}: deep backlog draining at {rate:g} component(s)/min"
        f"{projection}"
    )


def check(report: dict[str, Any]) -> None:
    """Fail the acceptance on any breach, naming it without naming content."""
    failures: list[str] = []
    for name in OPERATIONS:
        measured = report["operations"][name]
        if int(measured["samples"]) < MIN_SAMPLES_PER_OPERATION:
            failures.append(
                f"{name} samples={measured['samples']} < "
                f"{MIN_SAMPLES_PER_OPERATION}"
            )
        if float(measured["p50_ms"]) > P50_MS:
            failures.append(
                f"{name} p50={float(measured['p50_ms']):.1f}ms > {P50_MS:.1f}ms"
            )
        if float(measured["p90_ms"]) > P90_MS:
            failures.append(
                f"{name} p90={float(measured['p90_ms']):.1f}ms > {P90_MS:.1f}ms"
            )
    stale = int(report["read_your_write"]["stale"])
    if stale:
        failures.append(f"{stale} stale read(s) after an acknowledged write")
    uncovered = int(report["uncovered_full_receipts"])
    if uncovered != 0:
        failures.append(
            "uncovered full receipt accounting is "
            + ("unreadable" if uncovered < 0 else f"{uncovered} above zero")
        )
    if not report["post_burst_converged"]:
        failures.append(_convergence_failure(report))
    demanded = int(report.get("reconciliation_demanded", 0))
    if demanded:
        failures.append(
            f"{demanded} batch(es) failed closed into reconciliation demand"
        )
    if failures:
        raise SystemExit("live write acceptance failed: " + "; ".join(failures))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=TRANSPORTS, required=True)
    parser.add_argument(
        "--samples-per-operation", type=int, default=MIN_SAMPLES_PER_OPERATION
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--vault",
        type=Path,
        help="the disposable vault to run against; never resolved from the "
        "environment, because pointing this at a live cell is the one way it "
        "could do harm",
    )
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--connector-url")
    parser.add_argument(
        "--convergence-bound",
        type=float,
        default=CONVERGENCE_BOUND_SECONDS,
        help="seconds the post-burst backlog is given to drain; the design's "
        "own oldest-required-component alarm is the default, and a shorter "
        "one narrows the window rather than the assertion -- the run still "
        "fails, and still says which non-convergence it was",
    )
    args = parser.parse_args(argv)

    if args.vault is None:
        raise SystemExit(
            "live write acceptance requires an explicit --vault naming a "
            "disposable vault"
        )
    connector_url = args.connector_url or os.environ.get("EXOMEM_CONNECTOR_URL")
    if args.transport == "connector" and not connector_url:
        raise SystemExit(
            "live write acceptance failed: connector transport requires a "
            "--connector-url or EXOMEM_CONNECTOR_URL"
        )

    report = measure(
        args.vault,
        transport=args.transport,
        samples_per_operation=args.samples_per_operation,
        state_dir=args.state_dir or (args.vault.parent / "acceptance-state"),
        connector_url=connector_url,
        connector_token=os.environ.get("EXOMEM_CONNECTOR_TOKEN"),
        convergence_bound_seconds=args.convergence_bound,
    )
    print(json.dumps(report, sort_keys=True))
    if args.check:
        check(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
