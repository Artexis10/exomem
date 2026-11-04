#!/usr/bin/env python
"""Drive the scripted Adoption Studio journey through one long-lived stdio MCP session.

The journey is: start -> select -> plan -> apply (while work remains) -> finish, then a
second pass of the same five steps on the unchanged collection to measure duplicates.
Everything runs against a disposable vault and state root under ``--workdir``; the
collection is copied into the vault, so the originals under
``--collection`` are never opened for writing.

    python drive.py --collection C --workdir W --profile personal --python PY --log OUT.json

``personal`` leaves the media engines at their default (all on). ``cloud-like`` sets
``EXOMEM_MEDIA_ENGINES=documents,ocr`` (no speech), as the hosted image does.

``--existing-pages SPEC`` imports into a vault that already holds pages: the driver writes
the pages of SPEC (the ``*.vault-pages.json`` that the raw profiles write beside their key)
into the new vault before the first journey, and records their bytes in the log.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_session import LedgerSession, McpSession, isolated_env  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_FORMAT = 1
MAX_APPLY_PASSES = 6
MAX_PAGES = 2000  # follow-the-continuation guard, far above any real collection
MEDIA_POLL_S = 3.0
# Probe input only: kind/domain keys that exist in the shipped registry
# (src/exomem/vocabulary/packs/core), applied to whichever of these folders the collection has.
RULE_PROBE = {
    "reading/books": ("book", "media"),
    "reading/papers": ("research-report", "research"),
    "recipes": ("manual-documentation", "food"),
    "areas/travel": ("manual-documentation", "travel"),
}
PROFILE_ENV = {"personal": {}, "cloud-like": {"EXOMEM_MEDIA_ENGINES": "documents,ocr"}}
# A hosted cell serves the same journey as ``import_memory`` and ``read_import`` operations. The product's tool
# schema fixes which of the journey's actions are reads, so this is a closed set, not a vocabulary.
READ_ACTIONS = frozenset({"status", "work-item"})


def local_request(action: str, fields: dict[str, Any], detail: str) -> tuple[str, dict[str, Any]]:
    """The stdio server's one ``adoption_studio`` tool, selected by ``action``."""
    return "adoption_studio", {"action": action, "response_detail": detail, **fields}


def hosted_request(action: str, fields: dict[str, Any], detail: str) -> tuple[str, dict[str, Any]]:
    """A hosted cell's operation tools; ``response_detail`` is a top-level argument of the write tool only."""
    if action in READ_ACTIONS:
        return "read_import", {"operation": {"kind": action, **fields}}
    return "import_memory", {"operation": {"kind": action, **fields}, "response_detail": detail}


def place_collection(source: Path, target: Path) -> int:
    """Copy every fixture file as an ordinary file; return the file count."""
    count = 0
    for src in sorted(p for p in source.rglob("*") if p.is_file() or p.is_symlink()):
        dst = target / src.relative_to(source)
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Generic vault reads reject hardlink aliases, so the fixture needs its own files.
        shutil.copy2(src, dst)
        count += 1
    return count


def every_other_file(collection_dir: Path, name: str) -> list[str]:
    """Vault-relative path of each visible non-markdown file: what an agent that insists on all files names."""
    return sorted(
        f"{name}/{path.relative_to(collection_dir).as_posix()}"
        for path in collection_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() != ".md"
        and not any(part.startswith(".") for part in path.relative_to(collection_dir).parts)
    )


def seed_existing_pages(spec_file: Path, vault: Path, today: dt.date) -> dict[str, Any]:
    """Write the spec's pages into the vault; return what was written, for the scorer to compare.

    ``{today}`` in a path or a body is the day of this drive, the day that the server dates its pages.
    """
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    rows = []
    for page in spec["pages"]:
        rel = page["path"].replace("{today}", today.isoformat())
        data = page["content"].replace("{today}", today.isoformat()).encode("utf-8")
        target = vault / rel
        if target.exists():
            raise SystemExit(f"existing page {rel} is already in the new vault")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        rows.append({"id": page["id"], "path": rel, "bytes": len(data),
                     "sha256": hashlib.sha256(data).hexdigest()})
    return {"seeded": True, "spec": spec_file.name, "day": today.isoformat(), "pages": rows}


def init_vault(python: Path, env: dict[str, str], vault: Path, state: Path) -> dict[str, Any]:
    """Scaffold the Knowledge Base through the public CLI: the server refuses to boot without it."""
    started = time.perf_counter()
    done = subprocess.run(
        [str(python), "-m", "exomem", "init", "--vault", str(vault)],
        env=env, cwd=str(state), capture_output=True, text=True, check=False, timeout=600,
    )
    return {
        "command": "python -m exomem init --vault <vault>",
        "exit_code": done.returncode,
        "wall_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "stderr_tail": done.stderr[-300:],
    }


def run_object(data: Any) -> dict[str, Any]:
    """The run document: ``response_detail=full`` carries it under ``diagnostics``."""
    if isinstance(data, dict):
        inner = data.get("diagnostics")
        if isinstance(inner, dict) and isinstance(inner.get("leaf_result"), dict):
            inner = inner["leaf_result"]
        return inner if isinstance(inner, dict) else data
    return {}


def find_field(data: Any, name: str) -> Any:
    """Read ``name`` from the result envelope, then from its run document."""
    if not isinstance(data, dict):
        return None
    for source in (data, run_object(data)):
        if source.get(name) not in (None, "", [], {}):
            return source[name]
    return None


def counts_by_code(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        code = str(row.get("code") or "UNKNOWN")
        out[code] = out.get(code, 0) + 1
    return dict(sorted(out.items()))


def local_media_status() -> tuple[str, dict[str, Any]]:
    return "process_media", {"operation": "status"}


def hosted_media_status() -> tuple[str, dict[str, Any]]:
    return "read_media", {"operation": {"kind": "status"}}


class Journey:
    """One start->finish pass; accumulates what the product refused along the way."""

    def __init__(self, session: LedgerSession, name: str, collection: str, collection_dir: Path,
                 overrides: list[str], *, path: str | None = None,
                 request: Any = local_request, initialize_kb: bool = True):
        """``collection`` is the label; ``path`` is where the product reads it, vault-relative (default: the label)."""
        self.session = session
        self.overrides = overrides
        self.request = request
        self.initialize_kb = initialize_kb
        self.name = name
        self.collection = collection
        self.path = path or collection
        self.collection_dir = collection_dir
        self.run_id: str | None = None
        self.phase_ms: dict[str, float] = {}
        self.refusals: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.final: dict[str, Any] = {}
        self.notes: dict[str, Any] = {}

    async def adoption(self, step: str, args: dict[str, Any], *,
                       detail: str = "compact") -> tuple[str, Any, dict[str, Any]]:
        """Call one action. ``full`` carries the whole run, megabytes on a large collection,
        so only the steps that read the run ask for it."""
        started = time.perf_counter()
        key = step.split(":")[0]
        tool, request = self.request(key, args, detail)
        outcome, data, record = await self.session.call(tool, request, journey=self.name, step=step)
        self.phase_ms[key] = round(
            self.phase_ms.get(key, 0.0) + (time.perf_counter() - started) * 1000.0, 1
        )
        return outcome, data, record

    def note_refusals(self, step: str, data: Any) -> None:
        run = run_object(data)
        plan = run.get("plan") or {}
        for source, rows in (
            ("select.rejected", run.get("rejected") or []),
            ("plan.skipped", plan.get("skipped") or []),
        ):
            for row in rows:
                key = (source, str(row.get("path")), str(row.get("code")))
                self.refusals[key] = {"stage": source, "path": row.get("path"),
                                      "code": row.get("code"), "reason": row.get("reason")}
        for path, outcome in (run.get("outcomes") or {}).items():
            if outcome.get("status") == "failed":
                key = ("apply.failed", path, str(outcome.get("code")))
                self.refusals[key] = {"stage": "apply.failed", "path": path,
                                      "code": outcome.get("code"), "reason": outcome.get("reason")}

    async def follow(self, step: str, args: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any], int]:
        """Call ``step`` and repeat it with the returned ``continuation`` until none comes back.

        Each page asks for the compact reply, which carries no refusals. After the last page,
        or after the page that failed, one ``status`` call reads the run for the refusals and
        the plan. Returns the outcome, the call record that decided it, that run and the page
        count.
        """
        pages = 0
        outcome, record = "ok", {}
        while pages < MAX_PAGES:
            pages += 1
            outcome, data, record = await self.adoption(f"{step}:p{pages}", args)
            if outcome != "ok":
                break
            token = find_field(data, "continuation")
            if token is None:
                break
            args = {**args, "continuation": token}
        read, status, status_record = await self.adoption(
            f"status:after-{step}", {"run_id": self.run_id}, detail="full")
        run: dict[str, Any] = {}
        if read == "ok":
            self.note_refusals(step, status)
            run = run_object(status)
        elif outcome == "ok":
            outcome, record = read, status_record
        return outcome, record, run, pages

    async def run(self, *, collection_label: str, rules_state: dict[str, Any],
                  probe_rules: bool, include_rules: list[dict[str, Any]]) -> dict[str, Any]:
        """Execute the five steps; every step's raw outcome is in the session ledger."""
        include = [self.path]
        start_args: dict[str, Any] = {"path": self.path, "initialize_kb": self.initialize_kb}
        if collection_label != "unsupported":
            start_args["collection"] = self.collection
        outcome, data, record = await self.adoption("start", start_args)
        if outcome != "ok" and collection_label != "unsupported":
            rules_state["collection_label"] = "unsupported"
            rules_state["collection_label_error"] = {
                "error_code": record["error_code"], "error_message": record["error_message"]}
            start_args.pop("collection")
            outcome, data, record = await self.adoption("start:no-label", start_args)
        elif collection_label != "unsupported":
            rules_state["collection_label"] = "accepted"
        if outcome != "ok":
            return self._stop("start", record)
        self.run_id = find_field(data, "run_id")
        if self.run_id is None:
            return self._stop("start", {"error_message": "no run_id in the start result"})

        selected = False
        if include_rules:
            outcome, data, record = await self.adoption(
                "select:rules", {"run_id": self.run_id,
                                 "include": include_rules,
                                 "include_junk": False, **self._override_args()}, detail="full")
            rules_state["folder_kind_rules"] = "explicit"
            if outcome != "ok":
                return self._stop("select:rules", record)
            selected = True
        elif probe_rules and rules_state.get("folder_kind_rules") != "unsupported":
            rules = [
                {"path": f"{self.path}/{folder}", "source_kind": kind, "domain": domain}
                for folder, (kind, domain) in RULE_PROBE.items()
                if (self.collection_dir / folder).is_dir()
            ]
            rule_args = {"run_id": self.run_id, "include": [*rules, {"path": self.path}],
                         "include_junk": False, **self._override_args()}
            outcome, data, record = await self.adoption("select:rules", rule_args, detail="full")
            if outcome == "ok":
                rules_state["folder_kind_rules"] = "accepted"
                selected = True
            else:
                rules_state["folder_kind_rules"] = "unsupported"
                rules_state["folder_kind_rules_error"] = {
                    "error_code": record["error_code"], "error_message": record["error_message"]}
        if not selected:
            outcome, data, record = await self.adoption(
                "select", {"run_id": self.run_id, "include": include, "include_junk": False,
                           **self._override_args()}, detail="full")
            if outcome != "ok":
                return self._stop("select", record)
        self.note_refusals("select", data)

        outcome, record, run, pages = await self.follow("plan", {"run_id": self.run_id})
        if outcome != "ok":
            return self._stop("plan", record)
        plan = run.get("plan") or {}
        plan_id = plan.get("plan_id")
        self.notes["plan_pages"] = pages
        self.notes["plan_items_after_plan"] = len(plan.get("items") or [])

        retried_failed = False
        apply_passes = 0
        while apply_passes < MAX_APPLY_PASSES:
            apply_passes += 1
            args = {"run_id": self.run_id, "plan_id": plan_id}
            if retried_failed:
                args["retry_failed"] = True
            outcome, record, run, pages = await self.follow("apply", args)
            if outcome != "ok":
                return self._stop("apply", record)
            phase = run.get("phase")
            if phase == "partial" and not retried_failed:
                retried_failed = True  # one bounded retry of the failed subset, then move on
                continue
            if phase != "applying":
                break
        self.notes["apply_passes"] = apply_passes
        self.notes["retry_failed_used"] = retried_failed

        outcome, data, record = await self.adoption(
            "finish", {"run_id": self.run_id, "write_manifest": False}, detail="full")
        if outcome != "ok":
            return self._stop("finish", record)
        run = run_object(data)
        self.final = {
            "phase": run.get("phase"),
            "selected": len((run.get("selection") or {}).get("paths") or []),
            "plan_items": len((run.get("plan") or {}).get("items") or []),
            "outcome_status": counts_by_status(run.get("outcomes") or {}),
            "recall_check": (run.get("finish") or {}).get("recall_check"),
            # The product's own coverage report, kept verbatim so the scorer's
            # outside view can be compared with what finish told the agent.
            "coverage": find_field(data, "coverage"),
            "verified_unchanged": run.get("verified_unchanged"),
            "verified_total": run.get("verified_total"),
            "stopped_at": None,
        }
        return self.summary()

    def _override_args(self) -> dict[str, Any]:
        return {"overrides": self.overrides} if self.overrides else {}

    def _stop(self, step: str, record: dict[str, Any]) -> dict[str, Any]:
        self.final = {"stopped_at": step, "error_code": record.get("error_code"),
                      "error_message": record.get("error_message")}
        return self.summary()

    def summary(self) -> dict[str, Any]:
        refusals = sorted(self.refusals.values(), key=lambda r: (r["stage"], str(r["path"])))
        return {
            "name": self.name, "run_id": self.run_id, "phase_wall_ms": self.phase_ms,
            "refusals": refusals, "refusal_counts": counts_by_code(refusals),
            "notes": self.notes, **self.final,
        }


def counts_by_status(outcomes: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in outcomes.values():
        status = str(row.get("status"))
        out[status] = out.get(status, 0) + 1
    return dict(sorted(out.items()))


async def wait_for_media(session: LedgerSession, journey: str, deadline_s: float,
                         status_request: Any = local_media_status, *, jobs_only: bool = False) -> dict[str, Any]:
    """Poll ``process_media status`` until the job queue and index refresh drain, or time runs out.

    With ``jobs_only``, an idle job queue is enough: deferred index upserts can take longer than the deadline
    to drain, and a caller that judges only the queue would otherwise wait for them."""
    started = time.perf_counter()
    polls: list[dict[str, Any]] = []
    quiet = 0
    data: Any = None
    while True:
        tool, request = status_request()
        outcome, data, _ = await session.call(tool, request, journey=journey, step="media-status")
        counts = data.get("counts") if isinstance(data, dict) else None
        refresh = (data.get("index_refresh") or {}).get("count") if isinstance(data, dict) else None
        polls.append({"t_s": round(time.perf_counter() - started, 1), "outcome": outcome,
                      "counts": counts, "index_refresh": refresh})
        busy = bool(counts) and (counts.get("pending", 0) or counts.get("running", 0))
        quiet = quiet + 1 if outcome == "ok" and not busy and (jobs_only or not refresh) else 0
        if quiet >= 2 or (time.perf_counter() - started) > deadline_s:
            break
        await asyncio.sleep(MEDIA_POLL_S)
    waited = round(time.perf_counter() - started, 1)
    return {"converged": quiet >= 2, "idle_means": "jobs" if jobs_only else "jobs-and-index",
            "waited_s": waited, "deadline_s": deadline_s,
            "poll_count": len(polls), "polls_head": polls[:3], "polls_tail": polls[-3:],
            "final_status": data}


def kb_pages(vault: Path) -> list[str]:
    """Every markdown page under the Knowledge Base, vault-relative: a public file-level snapshot."""
    kb = vault / "Knowledge Base"
    return sorted(p.relative_to(vault).as_posix() for p in kb.rglob("*.md")) if kb.is_dir() else []


def git_head(server_root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        done = subprocess.run(["git", "-C", str(server_root), *args], capture_output=True,
                              text=True, check=False)
        return done.stdout.strip()

    return {"head": run("rev-parse", "HEAD"), "src_dirty": bool(run("status", "--porcelain", "--", "src"))}


async def drive(args: argparse.Namespace) -> dict[str, Any]:
    workdir = args.workdir.resolve()
    args.collection = args.collection.resolve()
    if not args.collection.is_dir() or not any(args.collection.iterdir()):
        raise SystemExit(f"--collection {args.collection} is not a populated directory")
    vault, state = workdir / "vault", workdir / "state"
    if workdir.exists() and any(workdir.iterdir()):
        raise SystemExit(f"--workdir {workdir} is not empty")
    for folder in (vault, state):
        folder.mkdir(parents=True, exist_ok=True)
    server_root = args.server_root.resolve()
    extra = {**PROFILE_ENV[args.profile]}
    env = isolated_env(state=state, vault=vault, server_root=server_root, extra=extra)
    placed = place_collection(args.collection, vault / args.collection_name)
    init = init_vault(args.python, env, vault, state)
    if init["exit_code"] != 0:
        raise SystemExit(f"exomem init failed: {init}")
    existing = (seed_existing_pages(args.existing_pages, vault, dt.date.today())
                if args.existing_pages else {"seeded": False})

    rules_state: dict[str, Any] = {"folder_kind_rules": "not-probed", "collection_label": "not-tried"}
    log: dict[str, Any] = {
        "format": LOG_FORMAT, "profile": args.profile, "label": args.label,
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "server": git_head(server_root), "python": str(args.python),
        "python_version": subprocess.run([str(args.python), "-c", "import platform;print(platform.python_version())"],
                                         capture_output=True, text=True, check=False).stdout.strip(),
        "paths": {"vault": str(vault), "state": str(state), "collection_name": args.collection_name},
        "server_root": str(server_root), "env_extra": extra,
        "include_rules": args.include_rule,
        "env": {k: v for k, v in env.items() if k.startswith(("EXOMEM_", "HF_"))},
        "search_mode_note": "EXOMEM_DISABLE_EMBEDDINGS=1: search_memory runs its lexical lane only",
        "collection_files_placed": placed, "init": init, "existing_vault": existing,
    }
    total_started = time.perf_counter()
    async with McpSession(python=args.python, state=state, env=env, timeout=args.timeout) as session:
        log["tools_discovered"] = session.tools
        journeys: list[dict[str, Any]] = []
        overrides = (every_other_file(args.collection, args.collection_name)
                     if args.select_every_file else [])
        log["select_every_file"] = {"enabled": args.select_every_file, "override_count": len(overrides)}
        first = Journey(session, "first", args.collection_name, args.collection, overrides)
        journeys.append(await first.run(collection_label="try", rules_state=rules_state,
                                        probe_rules=not args.no_rule_probe,
                                        include_rules=args.include_rule))
        media_first = await wait_for_media(session, "first", args.media_deadline, jobs_only=args.media_wait == "jobs")
        log["snapshot_after_first_run"] = kb_pages(vault)
        second = Journey(session, "rerun", args.collection_name, args.collection, overrides)
        journeys.append(await second.run(
            collection_label=rules_state.get("collection_label", "unsupported"),
            rules_state=rules_state,
            probe_rules=rules_state.get("folder_kind_rules") == "accepted",
            include_rules=args.include_rule))
        media_rerun = await wait_for_media(session, "rerun", args.media_deadline, jobs_only=args.media_wait == "jobs")
        log["tool_calls_total"] = len(session.calls)
        log["calls"] = session.calls
    log["total_wall_s"] = round(time.perf_counter() - total_started, 1)
    log.update(rules_state)
    log["journeys"] = journeys
    log["media"] = {"after_first_run": media_first, "after_rerun": media_rerun}
    refused: dict[str, int] = {}
    for call in session.calls:
        if call["outcome"] != "ok":
            label = f"{call['outcome']}:{call['error_code']}"
            refused[label] = refused.get(label, 0) + 1
    log["call_failures"] = dict(sorted(refused.items()))
    log["snapshot_after_rerun"] = kb_pages(vault)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.log.write_text(json.dumps(log, ensure_ascii=False, indent=1, default=str) + "\n",
                        encoding="utf-8")
    return log


def include_rule(value: str) -> dict[str, Any]:
    rule = json.loads(value)
    if not isinstance(rule, dict):
        raise argparse.ArgumentTypeError("an include rule must be a JSON object")
    return rule


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--collection", type=Path, required=True, help="generated collection directory")
    parser.add_argument("--workdir", type=Path, required=True, help="empty scratch directory for vault and state")
    parser.add_argument("--profile", choices=sorted(PROFILE_ENV), default="personal")
    parser.add_argument("--python", type=Path, default=Path(sys.executable),
                        help="interpreter with the server's dependencies installed")
    parser.add_argument("--server-root", type=Path, default=REPO_ROOT,
                        help="source tree to serve (must contain src/exomem)")
    parser.add_argument("--collection-name", default="Collection",
                        help="folder name the collection takes inside the vault, also the collection label")
    parser.add_argument("--media-deadline", type=float, default=300.0, help="seconds to wait for extraction")
    parser.add_argument("--media-wait", choices=("jobs-and-index", "jobs"), default="jobs-and-index",
                        help="what must be idle to stop waiting before the deadline")
    parser.add_argument("--timeout", type=float, default=900.0, help="per-call transport timeout, seconds")
    parser.add_argument("--no-rule-probe", action="store_true", help="skip the folder source_kind/domain probe")
    parser.add_argument("--include-rule", action="append", type=include_rule, default=[],
                        help="JSON selection rule; repeat to select more paths; replaces the default rules; refusal stops the run")
    parser.add_argument("--select-every-file", action="store_true",
                        help="also name every visible non-markdown file in `overrides` (observes per-file refusals)")
    parser.add_argument("--existing-pages", type=Path, default=None, metavar="SPEC",
                        help="page spec (<key>.vault-pages.json) to write into the vault before the import")
    parser.add_argument("--label", default="", help="free-text run label stored in the log")
    parser.add_argument("--log", type=Path, required=True, help="drive log JSON to write")
    args = parser.parse_args(argv)
    log = asyncio.run(drive(args))
    print(json.dumps({
        "log": str(args.log), "tool_calls": log["tool_calls_total"], "total_wall_s": log["total_wall_s"],
        "folder_kind_rules": log["folder_kind_rules"], "collection_label": log["collection_label"],
        "journeys": [{k: j.get(k) for k in ("name", "phase", "stopped_at", "outcome_status",
                                             "refusal_counts")} for j in log["journeys"]],
        "call_failures": log["call_failures"],
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
