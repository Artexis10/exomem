#!/usr/bin/env python
"""Product-flow benchmark for Exomem.

This is a local product harness, not an algorithm benchmark. It exercises the
CLI/MCP-facing flows a user or assistant actually depends on, then rates each
flow against Basic Memory's public product surface.

Usage:
  uv run python scripts/product_flow_benchmark.py
  uv run python scripts/product_flow_benchmark.py --json
  uv run python scripts/product_flow_benchmark.py --flow fresh_setup --flow search_recall
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASIC_MEMORY_ROOT = REPO_ROOT.parent / "basic-memory"

FLOW_ORDER = [
    "fresh_setup",
    "messy_vault_adoption",
    "search_recall",
    "write_remember",
    "source_preservation",
    "evidence_provenance",
    "schema_inference_validation",
    "graph_context_building",
    "review_stale_contradiction",
    "assistant_onboarding",
]

RATINGS = ("ahead", "comparable", "behind", "missing", "not_measured")
STATUSES = ("pass", "partial", "fail", "not_measured")


@dataclass
class CommandRun:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def as_dict(self) -> dict:
        return {
            "argv": self.argv,
            "returncode": self.returncode,
            "seconds": round(self.seconds, 3),
            "stdout_tail": _tail(self.stdout),
            "stderr_tail": _tail(self.stderr),
        }


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class FlowResult:
    id: str
    name: str
    status: str
    rating: str
    commands: list[CommandRun] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "rating": self.rating,
            "commands": [c.as_dict() for c in self.commands],
            "checks": [c.as_dict() for c in self.checks],
            "evidence": self.evidence,
            "gaps": self.gaps,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


@dataclass
class HarnessContext:
    repo_root: Path
    basic_memory_root: Path
    timeout_seconds: float
    keep_tmp: bool
    tmp_root: Path
    python: str = sys.executable

    def cli_env(self, vault: Path, home: Path | None = None) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("EXOMEM_", "KB_MCP_"))
        }
        env.update(
            {
                "EXOMEM_VAULT_PATH": str(vault),
                "EXOMEM_KB_DIRNAME": "Knowledge Base",
                "EXOMEM_DISABLE_EMBEDDINGS": "1",
                "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
                "EXOMEM_DISABLE_CLIP": "1",
                "EXOMEM_DISABLE_RELEVANCE_CHECK": "1",
                "EXOMEM_DISABLE_QUERY_LOG": "1",
                "EXOMEM_DISABLE_RANKING_CONFIG": "1",
                "EXOMEM_DISABLE_WARMUP": "1",
                "EXOMEM_DISABLE_FILE_WATCHER": "1",
                "EXOMEM_DISABLE_MODE_WATCH": "1",
                "EXOMEM_CONFIG_PATH": str((home or self.tmp_root) / "exomem-config.json"),
                "PYTHONUTF8": "1",
            }
        )
        if home is not None:
            env["HOME"] = str(home)
            if sys.platform == "win32":
                env["USERPROFILE"] = str(home)
        return env


class FlowRunner:
    def __init__(self, ctx: HarnessContext):
        self.ctx = ctx
        self.commands: list[CommandRun] = []

    def run(
        self,
        vault: Path,
        *args: str,
        home: Path | None = None,
        expect_ok: bool = True,
    ) -> tuple[CommandRun, dict | None]:
        argv = [self.ctx.python, "-m", "exomem", *args]
        t0 = time.perf_counter()
        proc = subprocess.run(
            argv,
            cwd=self.ctx.repo_root,
            env=self.ctx.cli_env(vault, home),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.ctx.timeout_seconds,
        )
        run = CommandRun(
            argv=argv,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            seconds=time.perf_counter() - t0,
        )
        self.commands.append(run)
        payload = _json_payload(proc.stdout)
        if expect_ok and proc.returncode != 0:
            return run, payload
        return run, payload

    def flow(
        self,
        *,
        flow_id: str,
        name: str,
        rating: str,
        checks: list[Check],
        evidence: list[str],
        gaps: list[str] | None = None,
        elapsed: float = 0.0,
    ) -> FlowResult:
        status = _status_from_checks(checks)
        return FlowResult(
            id=flow_id,
            name=name,
            status=status,
            rating=rating,
            commands=list(self.commands),
            checks=checks,
            evidence=evidence,
            gaps=gaps or [],
            elapsed_seconds=elapsed,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--flow",
        action="append",
        choices=FLOW_ORDER,
        help="flow id to run; repeatable. Default: all flows.",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--keep-tmp",
        action="store_true",
        help="keep temporary benchmark vaults for inspection",
    )
    parser.add_argument(
        "--tmp-root",
        default=None,
        help="directory under which to create benchmark scratch data (default: .pytest-tmp/product-flow-benchmark)",
    )
    parser.add_argument(
        "--basic-memory-root",
        default=str(DEFAULT_BASIC_MEMORY_ROOT),
        help="sibling Basic Memory checkout used only for public product-surface detection",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="timeout per CLI command (default: 60)",
    )
    args = parser.parse_args(argv)

    selected = args.flow or FLOW_ORDER
    tmp_base = (
        Path(args.tmp_root).expanduser()
        if args.tmp_root
        else REPO_ROOT / ".pytest-tmp" / "product-flow-benchmark"
    )
    tmp_base.mkdir(parents=True, exist_ok=True)
    tmp_parent = tmp_base / f"run-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    tmp_parent.mkdir(parents=True, exist_ok=False)
    ctx = HarnessContext(
        repo_root=REPO_ROOT,
        basic_memory_root=Path(args.basic_memory_root),
        timeout_seconds=args.timeout_seconds,
        keep_tmp=args.keep_tmp,
        tmp_root=tmp_parent,
    )
    try:
        report = run_benchmark(ctx, selected)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(render_text_report(report))
        return 0
    finally:
        if args.keep_tmp:
            print(f"\nkept temporary benchmark root: {tmp_parent}")
        else:
            shutil.rmtree(tmp_parent, ignore_errors=True)


def run_benchmark(ctx: HarnessContext, selected: Iterable[str] = FLOW_ORDER) -> dict:
    selected_set = set(selected)
    unknown = selected_set.difference(FLOW_ORDER)
    if unknown:
        raise ValueError(f"unknown flow id(s): {', '.join(sorted(unknown))}")

    bm_reference = basic_memory_reference(ctx.basic_memory_root)
    flows: list[FlowResult] = []
    started = time.perf_counter()
    for flow_id in FLOW_ORDER:
        if flow_id not in selected_set:
            continue
        runner = FlowRunner(ctx)
        flow_fn = _FLOW_FUNCS[flow_id]
        t0 = time.perf_counter()
        result = flow_fn(ctx, runner, bm_reference)
        result.elapsed_seconds = time.perf_counter() - t0
        flows.append(result)

    summary = summarize_flows(flows)
    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "repo": str(ctx.repo_root),
        "flows": [f.as_dict() for f in flows],
        "summary": summary,
        "basic_memory_reference": bm_reference,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def summarize_flows(flows: list[FlowResult]) -> dict:
    by_rating = {rating: 0 for rating in RATINGS}
    by_status = {status: 0 for status in STATUSES}
    for flow in flows:
        by_rating[flow.rating] = by_rating.get(flow.rating, 0) + 1
        by_status[flow.status] = by_status.get(flow.status, 0) + 1
    return {
        "total": len(flows),
        "by_rating": by_rating,
        "by_status": by_status,
        "failures": [f.id for f in flows if f.status == "fail"],
    }


def render_text_report(report: dict) -> str:
    lines = [
        "# Exomem product flow benchmark",
        "",
        f"Generated: {report['generated_at']}",
        f"Repo: {report['repo']}",
        "",
        "## Summary",
        "",
    ]
    ratings = report["summary"]["by_rating"]
    lines.append(
        "Ratings: "
        + ", ".join(f"{key}={ratings.get(key, 0)}" for key in RATINGS)
    )
    lines.append("")
    lines.extend(["## Flows", ""])
    for flow in report["flows"]:
        lines.append(
            f"- {flow['id']}: {flow['status']} / {flow['rating']} "
            f"({flow['elapsed_seconds']:.2f}s)"
        )
        for check in flow["checks"]:
            marker = "OK" if check["ok"] else "FAIL"
            lines.append(f"  - {marker}: {check['name']} - {check['detail']}")
        for gap in flow["gaps"]:
            lines.append(f"  - Gap: {gap}")
        if flow["evidence"]:
            lines.append(f"  - Evidence: {'; '.join(flow['evidence'])}")
    lines.extend(["", "## Basic Memory product surface observed", ""])
    for item in report["basic_memory_reference"]["observed"]:
        lines.append(f"- {item}")
    return "\n".join(lines)


def basic_memory_reference(root: Path) -> dict:
    """Read public/product-facing Basic Memory surface hints, not implementation."""
    observed: list[str] = []
    missing: list[str] = []
    files = [
        root / "README.md",
        root / "docs" / "ai-assistant-guide-extended.md",
        root / "docs" / "specs" / "SPEC-SCHEMA.md",
    ]
    text = ""
    for path in files:
        try:
            text += "\n" + path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            missing.append(str(path))

    signals = {
        "cloud/local onboarding": ["Start free trial", "uv tool install basic-memory"],
        "cross-device sync": ["Cross-device sync", "Bidirectional sync"],
        "write/read/search MCP tools": ["write_note", "read_note", "search_notes"],
        "context graph tools": ["build_context", "canvas"],
        "schema tools": ["schema_infer", "schema_validate", "schema_diff"],
        "importers": ["import claude conversations", "import chatgpt", "import memory-json"],
        "recent activity": ["recent_activity"],
        "multi-project routing": ["list_memory_projects", "project set-cloud"],
    }
    for label, needles in signals.items():
        found = [needle for needle in needles if needle in text]
        if found:
            observed.append(f"{label}: {', '.join(found)}")
    if root.exists():
        observed.append(f"public checkout inspected at {root}")
    else:
        missing.append(str(root))
    return {"root": str(root), "observed": observed, "missing": missing}


def flow_fresh_setup(ctx: HarnessContext, runner: FlowRunner, bm: dict) -> FlowResult:
    vault = _mk_vault(ctx, "fresh-setup")
    home = _mk_home(ctx, "fresh-setup")
    run, payload = runner.run(
        vault,
        "setup",
        "--vault",
        str(vault),
        "--yes",
        "--lean",
        "--no-hooks",
        "--skip-claude-register",
        home=home,
    )
    kb = vault / "Knowledge Base"
    skill = home / ".claude" / "skills" / "exomem" / "SKILL.md"
    checks = [
        Check("setup exits successfully", run.ok, f"exit={run.returncode}"),
        Check("Knowledge Base scaffold exists", (kb / "_Schema" / "SKILL.md").is_file(), str(kb)),
        Check("skill install isolated to temp HOME", skill.is_file(), str(skill)),
    ]
    evidence = [
        "Exomem setup can create a fresh governed vault from the CLI.",
        "Setup still does more than a pure smoke: skill install is part of the flow.",
    ]
    gaps = [
        "Basic Memory has a shorter public local install path and a cloud no-install path.",
        "Exomem setup remains more configuration-heavy than Basic Memory's first-run story.",
    ]
    return runner.flow(
        flow_id="fresh_setup",
        name="Fresh vault setup",
        rating="behind",
        checks=checks,
        evidence=evidence,
        gaps=gaps,
    )


def flow_messy_vault_adoption(ctx: HarnessContext, runner: FlowRunner, bm: dict) -> FlowResult:
    vault = _mk_vault(ctx, "messy-adoption")
    _seed_messy_vault(vault)
    before = _snapshot_files(vault)
    overview_run, overview_payload = runner.run(vault, "overview", "--json")
    adopt_run, adopt_payload = runner.run(vault, "adopt", ".", "--mode", "scan-only", "--json")
    after = _snapshot_files(vault)
    data = (adopt_payload or {}).get("data", {})
    totals = data.get("summary", {}).get("totals", {})
    checks = [
        Check("overview exits successfully", overview_run.ok, f"exit={overview_run.returncode}"),
        Check("scan-only adoption exits successfully", adopt_run.ok, f"exit={adopt_run.returncode}"),
        Check("scan-only does not mutate existing files", before == after, f"{len(before)} files before"),
        Check("messy markdown counted", int(totals.get("markdown", 0) or 0) >= 2, str(totals)),
    ]
    return runner.flow(
        flow_id="messy_vault_adoption",
        name="Existing messy vault adoption",
        rating="behind",
        checks=checks,
        evidence=[
            "The product model has an explicit scan-only adoption contract.",
            "The direct CLI path currently rejects an uninitialized messy vault before it can scan.",
        ],
        gaps=[
            "Fix CLI/MCP adoption so `adopt` and `overview` can scan a pre-init vault directly, matching setup wizard behavior.",
            "Basic Memory has richer importers for common AI exports; Exomem adoption is safer in design but less usable today.",
        ],
    )


def flow_search_recall(ctx: HarnessContext, runner: FlowRunner, bm: dict) -> FlowResult:
    vault = _prepared_vault(ctx, runner, "search-recall")
    path = _write_recall_note(vault)
    run, payload = runner.run(
        vault,
        "find",
        "rotating token",
        "--mode",
        "keyword",
        "--limit",
        "5",
        "--json",
    )
    hits = (payload or {}).get("data") or []
    paths = [hit.get("path", "") for hit in hits if isinstance(hit, dict)]
    checks = [
        Check("find exits successfully", run.ok, f"exit={run.returncode}"),
        Check("seed note is recalled", path.as_posix() in paths, ", ".join(paths)),
        Check("results carry paths for citation", all(".md" in p for p in paths[:1]), paths[0] if paths else "none"),
    ]
    return runner.flow(
        flow_id="search_recall",
        name="Search/recall",
        rating="comparable",
        checks=checks,
        evidence=[
            "Exomem supports keyword and hybrid-facing recall; lean benchmark uses keyword to avoid model downloads.",
            "Find results expose vault-relative paths.",
        ],
        gaps=[
            "Basic Memory search docs expose text/vector/hybrid modes and richer result snippets as a polished product surface.",
        ],
    )


def flow_write_remember(ctx: HarnessContext, runner: FlowRunner, bm: dict) -> FlowResult:
    vault = _prepared_vault(ctx, runner, "write-remember")
    add_run, add_payload = runner.run(
        vault,
        "add",
        "--content",
        "Session capture: durable benchmark memory should cite raw source material.",
        "--source-type",
        "session",
        "--title",
        "Benchmark source capture",
        "--why-captured",
        "product flow benchmark",
        "--json",
    )
    source_path = ((add_payload or {}).get("data") or {}).get("path", "")
    note_run, note_payload = runner.run(
        vault,
        "note",
        "--note-type",
        "insight",
        "--title",
        "Benchmark source-backed insight",
        "--content",
        "# Benchmark source-backed insight\n\n## Claim\n\nDurable product memories should cite raw source material.\n\n## Why it holds\n\nThe benchmark writes a raw Source first, then a compiled note that cites it.\n",
        "--field",
        f"sources={source_path}",
        "--json",
    )
    note_path = ((note_payload or {}).get("data") or {}).get("path", "")
    source_text = (vault / source_path).read_text(encoding="utf-8", errors="replace") if source_path else ""
    checks = [
        Check("add source succeeds", add_run.ok and bool(source_path), source_path or f"exit={add_run.returncode}"),
        Check("note succeeds", note_run.ok and bool(note_path), note_path or f"exit={note_run.returncode}"),
        Check("note cites source", source_path.removesuffix(".md") in (vault / note_path).read_text(encoding="utf-8", errors="replace") if note_path else False, source_path),
        Check("source has ingestion backlink", "ingested_into" in source_text and "benchmark-source-backed-insight" in source_text, "source frontmatter backlink"),
    ]
    return runner.flow(
        flow_id="write_remember",
        name="Write/remember",
        rating="ahead",
        checks=checks,
        evidence=[
            "Exomem separates raw Sources from compiled notes and updates source-to-note provenance.",
        ],
        gaps=[
            "Basic Memory's `write_note` is simpler for an assistant; Exomem's governed write is safer but more verbose.",
        ],
    )


def flow_source_preservation(ctx: HarnessContext, runner: FlowRunner, bm: dict) -> FlowResult:
    vault = _mk_vault(ctx, "source-preservation")
    _seed_messy_vault(vault)
    legacy = vault / "Legacy" / "meeting notes.md"
    before_hash = _sha256(legacy)
    setup_runner = FlowRunner(ctx)
    setup_runner.run(
        vault,
        "init",
        "--vault",
        str(vault),
        expect_ok=True,
    )
    runner.commands.extend(setup_runner.commands)
    run, payload = runner.run(
        vault,
        "adopt",
        ".",
        "--mode",
        "copy-as-sources",
        "--selected-paths",
        "Legacy/meeting notes.md",
        "--json",
    )
    data = (payload or {}).get("data", {})
    copied = (((data.get("copy") or {}).get("copied_sources")) or [])
    source_path = copied[0]["source_path"] if copied else ""
    source_text = (vault / source_path).read_text(encoding="utf-8", errors="replace") if source_path else ""
    checks = [
        Check("copy-as-sources succeeds", run.ok, f"exit={run.returncode}"),
        Check("original file unchanged", before_hash == _sha256(legacy), str(legacy)),
        Check("imported source created", bool(source_path) and (vault / source_path).is_file(), source_path),
        Check("original path preserved", "imported_from: Legacy/meeting notes.md" in source_text, source_path),
        Check("original sha256 preserved", before_hash in source_text, before_hash),
    ]
    return runner.flow(
        flow_id="source_preservation",
        name="Source preservation",
        rating="ahead",
        checks=checks,
        evidence=[
            "Exomem can copy selected legacy files as Sources while preserving original path and SHA-256.",
        ],
        gaps=[
            "Basic Memory importers cover more source formats, but Exomem's preservation contract is stricter.",
        ],
    )


def flow_evidence_provenance(ctx: HarnessContext, runner: FlowRunner, bm: dict) -> FlowResult:
    vault = _prepared_vault(ctx, runner, "evidence-provenance")
    preserve_run, preserve_payload = runner.run(
        vault,
        "preserve",
        "--scope",
        "warranty",
        "--category",
        "receipts",
        "--filename",
        "laptop-receipt.txt",
        "--content",
        "Receipt #R-1001 for benchmark laptop.",
        "--description",
        "Benchmark receipt",
        "--json",
    )
    evidence_path = ((preserve_payload or {}).get("data") or {}).get("path", "")
    note_run, note_payload = runner.run(
        vault,
        "note",
        "--note-type",
        "insight",
        "--title",
        "Benchmark warranty proof",
        "--content",
        "# Benchmark warranty proof\n\n## Claim\n\nThe benchmark receipt is preserved as proof. <!-- evidence:R-1001 -->\n\n## Why it holds\n\nThe Evidence artifact stores the receipt text separately from this compiled claim.\n",
        "--json",
    )
    prov_run, prov_payload = runner.run(
        vault,
        "provenance_report",
        "--key",
        "evidence",
        "--value",
        "R-1001",
        "--json",
    )
    findings_data = (prov_payload or {}).get("data") or []
    findings = findings_data.get("findings", []) if isinstance(findings_data, dict) else findings_data
    checks = [
        Check("preserve succeeds", preserve_run.ok and bool(evidence_path), evidence_path),
        Check("evidence artifact exists", bool(evidence_path) and (vault / evidence_path).is_file(), evidence_path),
        Check("provenance note succeeds", note_run.ok, f"exit={note_run.returncode}"),
        Check("provenance report finds marker", bool(findings), json.dumps(findings[:1], default=str)),
    ]
    return runner.flow(
        flow_id="evidence_provenance",
        name="Evidence/provenance",
        rating="ahead",
        checks=checks,
        evidence=[
            "Exomem has a distinct Evidence tree plus read-only provenance reporting.",
        ],
        gaps=[
            "The provenance tag mechanism is powerful but still too hidden for ordinary users.",
        ],
    )


def flow_schema_inference_validation(ctx: HarnessContext, runner: FlowRunner, bm: dict) -> FlowResult:
    checks = [
        Check(
            "Exomem exposes no schema inference CLI flow",
            False,
            "No product-facing `schema_infer`/`schema_validate` equivalent is in docs/capabilities.",
        )
    ]
    result = FlowResult(
        id="schema_inference_validation",
        name="Schema inference/validation",
        status="not_measured",
        rating="missing",
        commands=[],
        checks=checks,
        evidence=[
            "Basic Memory public docs list `schema_infer`, `schema_validate`, and `schema_diff`.",
            "Exomem has internal page-type validation, but not a user-facing schema inference/validation product flow.",
        ],
        gaps=[
            "Add a product-facing schema/audit flow only if it fits Exomem's governed source/evidence model.",
        ],
        elapsed_seconds=0.0,
    )
    return result


def flow_graph_context_building(ctx: HarnessContext, runner: FlowRunner, bm: dict) -> FlowResult:
    vault = _prepared_vault(ctx, runner, "graph-context")
    source = _write_context_notes(vault)
    suggest_run, suggest_payload = runner.run(
        vault,
        "suggest_links",
        "--draft-title",
        "Rotating token rollout",
        "--draft-body",
        "This draft discusses rotating tokens and auth memory.",
        "--limit",
        "5",
        "--json",
    )
    inbound_run, inbound_payload = runner.run(
        vault,
        "list_inbound_links",
        source.as_posix(),
        "--json",
    )
    pack_run, pack_payload = runner.run(
        vault,
        "find",
        "rotating token",
        "--mode",
        "keyword",
        "--pack",
        "--json",
    )
    suggestions = (suggest_payload or {}).get("data") or []
    inbound_count = (((inbound_payload or {}).get("data") or {}).get("count")) or 0
    pack = ((pack_payload or {}).get("data") or {}).get("pack")
    checks = [
        Check("suggest_links succeeds", suggest_run.ok, f"exit={suggest_run.returncode}"),
        Check("suggest_links returns a list", isinstance(suggestions, list), json.dumps(suggestions[:1], default=str)),
        Check("list_inbound_links detects links", inbound_run.ok and inbound_count >= 1, f"count={inbound_count}"),
        Check("find(pack=true) returns pack", pack_run.ok and isinstance(pack, dict), "pack present" if isinstance(pack, dict) else "missing"),
    ]
    return runner.flow(
        flow_id="graph_context_building",
        name="Graph/context building",
        rating="behind",
        checks=checks,
        evidence=[
            "Exomem has graph primitives (`suggest_links`, inbound links, context packs).",
        ],
        gaps=[
            "Basic Memory exposes `build_context` and `canvas` as more obvious assistant/user workflows.",
            "Exomem's context building is capable but fragmented across multiple tools.",
        ],
    )


def flow_review_stale_contradiction(ctx: HarnessContext, runner: FlowRunner, bm: dict) -> FlowResult:
    vault = _prepared_vault(ctx, runner, "review")
    add_run, add_payload = runner.run(
        vault,
        "add",
        "--content",
        "Unprocessed source: review queues should surface captured material that has not been compiled.",
        "--source-type",
        "session",
        "--title",
        "Unprocessed benchmark source",
        "--json",
    )
    source_path = ((add_payload or {}).get("data") or {}).get("path", "")
    audit_run, audit_payload = runner.run(vault, "audit", "--json")
    attention_run, attention_payload = runner.run(vault, "attention", "--limit", "5", "--json")
    propose_run, propose_payload = runner.run(
        vault,
        "propose_compilation",
        "--sources",
        source_path,
        "--json",
    )
    audit_findings = (((audit_payload or {}).get("data") or {}).get("findings")) or []
    proposal = (propose_payload or {}).get("data") or {}
    checks = [
        Check("seed source succeeds", add_run.ok and bool(source_path), source_path),
        Check("audit succeeds", audit_run.ok, f"exit={audit_run.returncode}"),
        Check("audit surfaces review data", isinstance(audit_findings, list), f"{len(audit_findings)} findings"),
        Check("attention succeeds", attention_run.ok, f"exit={attention_run.returncode}"),
        Check("propose_compilation returns scaffold", propose_run.ok and bool(proposal.get("outline_markdown")), json.dumps(proposal, default=str)[:240]),
    ]
    return runner.flow(
        flow_id="review_stale_contradiction",
        name="Review/stale/contradiction workflow",
        rating="ahead",
        checks=checks,
        evidence=[
            "Exomem has first-class audit, attention, stale review, contradiction, and compilation scaffolding surfaces.",
        ],
        gaps=[
            "The review system is strong but needs a clearer daily product workflow and fewer tool names.",
        ],
    )


def flow_assistant_onboarding(ctx: HarnessContext, runner: FlowRunner, bm: dict) -> FlowResult:
    vault = _prepared_vault(ctx, runner, "assistant-onboarding")
    bootstrap_run, bootstrap_payload = runner.run(
        vault,
        "bootstrap",
        "--workflow",
        "product quality benchmark",
        "--json",
    )
    demo_vault = ctx.tmp_root / "assistant-onboarding" / "demo-vault"
    demo_vault.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        ctx.repo_root / "src" / "exomem" / "_sample_vault" / "Knowledge Base",
        demo_vault / "Knowledge Base",
        dirs_exist_ok=True,
    )
    demo_run, demo_payload = runner.run(vault, "demo", "--vault", str(demo_vault), "--json")
    contract = (bootstrap_payload or {}).get("data") or {}
    demo = demo_payload or {}
    checks = [
        Check("bootstrap succeeds", bootstrap_run.ok, f"exit={bootstrap_run.returncode}"),
        Check("bootstrap exposes front-door actions", "front_door_actions" in contract, "front_door_actions"),
        Check("bootstrap explains adoption", "adopt_existing_vault" in (contract.get("tool_defaults") or {}), "tool_defaults.adopt_existing_vault"),
        Check("demo succeeds", demo_run.ok and demo.get("success") is True, json.dumps(demo, default=str)),
    ]
    return runner.flow(
        flow_id="assistant_onboarding",
        name="Assistant onboarding",
        rating="comparable",
        checks=checks,
        evidence=[
            "Exomem exposes `bootstrap` and `demo` as assistant-facing onboarding surfaces.",
            "Basic Memory has broader packaged plugins/skills and public docs across clients.",
        ],
        gaps=[
            "Exomem's assistant onboarding is technically explicit, but Basic Memory's ecosystem packaging is ahead.",
        ],
    )


_FLOW_FUNCS: dict[str, Callable[[HarnessContext, FlowRunner, dict], FlowResult]] = {
    "fresh_setup": flow_fresh_setup,
    "messy_vault_adoption": flow_messy_vault_adoption,
    "search_recall": flow_search_recall,
    "write_remember": flow_write_remember,
    "source_preservation": flow_source_preservation,
    "evidence_provenance": flow_evidence_provenance,
    "schema_inference_validation": flow_schema_inference_validation,
    "graph_context_building": flow_graph_context_building,
    "review_stale_contradiction": flow_review_stale_contradiction,
    "assistant_onboarding": flow_assistant_onboarding,
}


def _prepared_vault(ctx: HarnessContext, runner: FlowRunner, name: str) -> Path:
    vault = _mk_vault(ctx, name)
    runner.run(vault, "init", "--vault", str(vault))
    return vault


def _mk_vault(ctx: HarnessContext, name: str) -> Path:
    vault = ctx.tmp_root / name / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    return vault


def _mk_home(ctx: HarnessContext, name: str) -> Path:
    home = ctx.tmp_root / name / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _seed_messy_vault(vault: Path) -> None:
    (vault / "Legacy").mkdir(parents=True, exist_ok=True)
    (vault / "Archive").mkdir(parents=True, exist_ok=True)
    (vault / "Legacy" / "meeting notes.md").write_text(
        "# Meeting notes\n\nAuth launch needs rotating token rollout.\n",
        encoding="utf-8",
    )
    (vault / "Legacy" / "meeting notes 1.md").write_text(
        "# Meeting notes conflict\n\nA sync-conflict-like duplicate.\n",
        encoding="utf-8",
    )
    (vault / "Archive" / "receipt.txt").write_text(
        "Receipt #R-1001\n",
        encoding="utf-8",
    )
    (vault / "empty.md").write_text("", encoding="utf-8")


def _write_recall_note(vault: Path) -> Path:
    rel = Path("Knowledge Base/Notes/Insights/rotating-token-rollout.md")
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        "title: Rotating token rollout\n"
        "status: active\n"
        "created: 2026-07-08\n"
        "updated: 2026-07-08\n"
        "tags: [benchmark]\n"
        "---\n\n"
        "# Rotating token rollout\n\n"
        "## Claim\n\n"
        "The rotating token launch needs a rollback owner and audit trail.\n",
        encoding="utf-8",
    )
    return rel


def _write_context_notes(vault: Path) -> Path:
    source = _write_recall_note(vault)
    rel = Path("Knowledge Base/Notes/Research/Auth/auth-memory.md")
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: research-note\n"
        "title: Auth memory\n"
        "project: auth\n"
        "status: active\n"
        "created: 2026-07-08\n"
        "updated: 2026-07-08\n"
        "---\n\n"
        "# Auth memory\n\n"
        "## Question\n\n"
        "How should launch context connect?\n\n"
        "## Findings\n\n"
        f"The rollout depends on [[{source.as_posix()}]].\n",
        encoding="utf-8",
    )
    return source


def _status_from_checks(checks: list[Check]) -> str:
    if not checks:
        return "not_measured"
    passed = sum(1 for c in checks if c.ok)
    if passed == len(checks):
        return "pass"
    if passed == 0:
        return "fail"
    return "partial"


def _json_payload(stdout: str) -> dict | None:
    for line in reversed([line.strip() for line in stdout.splitlines() if line.strip()]):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _tail(text: str, limit: int = 1000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _snapshot_files(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        out[path.relative_to(root).as_posix()] = _sha256(path)
    return out


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
