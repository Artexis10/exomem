"""Workflow journeys J1/J2: ordered product-CLI steps + deterministic checks.

Framework follows scripts/product_flow_benchmark.py (CommandRun dataclass,
runner with ``run(vault, *args)`` -> (CommandRun, payload), check list ->
status), adapted to journey scoring: steps_count, checks passed/failed,
manual_interventions (always 0 — every step is a scripted subprocess).

CLI envelope contracts were read by RUNNING each command against a scratch
vault first (2026-07-31, this worktree, lexical env below):

- ``remember --json`` -> ``{"success": true, "data": {"ok": true, "status":
  "committed", "path": "Knowledge Base/Notes/...", ...}}``
- ``replace_memory <old> --json`` -> ``data.paths == [old_path, new_path]``
- ``review_memory --mode evolution --path <p> --json`` ->
  ``data.timelines[0]`` with ``span.n_versions``, ordered ``versions`` rows
  ``{path, title, status: superseded|active, transition}`` and
  ``chain_id`` = newest path, ``topic_anchor`` = oldest path
- ``ask_memory <q> --json`` (product defaults: hybrid + prefer-active) ->
  ``data`` = ranked list of ``{path, type, title, updated, status?,
  superseded_by?, ref}`` where superseded pages carry ``status`` and rank
  below active pages sharing the vocabulary
- ``read_memory <p> --json`` -> ``data.frontmatter`` (``status``,
  ``superseded_by``, ``sources`` as ``[[...]]`` wikilinks WITHOUT the .md
  suffix) + ``data.body``
- ``capture_source --json`` -> ``data.path`` (date-prefixed under
  ``Knowledge Base/Sources/...`` — never hardcode, read the envelope)
- ``maintain_memory --mode audit --json`` -> ``data.findings`` rows with
  ``category`` (e.g. relation_debt, unprocessed_source; a contradiction
  would surface as a contradiction-family category) + ``data.summary``

Product-contract notes encoded in the journey bodies:

- The FIRST compiled note in a fresh vault commits under the automatic
  bootstrap relation disposition (src/exomem/semantic_contract.py lines
  2618-2633); replacements qualify via the auto-written ``supersedes``
  frontmatter relation (core-relations.yaml: supersedes origins include
  frontmatter), so J1/J2 never need the validate-then-commit dance.
- Bodies include one compact semantic unit under ``## Observations``
  (semantic authoring contract surfaced in the ``remember --help`` text) so
  commits are warning-free.
- Retrieval runs the product-default hybrid lane with embeddings disabled
  (lexical degradation): a query matches only when its stems appear in the
  page, so every paraphrase below draws exclusively from the notes'
  vocabulary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"
COMMAND_TIMEOUT_SECONDS = 60.0
KB_DIRNAME = "Knowledge Base"


def journey_env(vault: Path, workdir: Path) -> dict[str, str]:
    """Isolated deterministic env for product-CLI subprocesses.

    Copies lexical_profile()'s settings (benchmarks/membench/adapters/
    exomem_local.py lines 43-62) and adds the vault/config/home isolation the
    product-flow harness uses (scripts/product_flow_benchmark.py lines
    117-144). EXOMEM_VAULT_PATH is ALWAYS the benchmark temp vault.
    """
    from membench.adapters.exomem_local import lexical_profile

    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(workdir),
        "EXOMEM_VAULT_PATH": str(vault),
        "EXOMEM_KB_DIRNAME": KB_DIRNAME,
        "EXOMEM_CONFIG_PATH": str(workdir / "exomem-config.json"),
        "PYTHONPATH": str(SRC_DIR),
        "PYTHONUTF8": "1",
        **lexical_profile().settings,
    }


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
            "stdout_tail": self.stdout.strip()[-800:],
            "stderr_tail": self.stderr.strip()[-800:],
        }


@dataclass
class JourneyCheck:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class JourneyResult:
    id: str
    name: str
    steps_count: int
    checks: list[JourneyCheck] = field(default_factory=list)
    commands: list[CommandRun] = field(default_factory=list)
    manual_interventions: int = 0
    elapsed_seconds: float = 0.0

    @property
    def passed(self) -> list[str]:
        return [c.name for c in self.checks if c.ok]

    @property
    def failed(self) -> list[str]:
        return [c.name for c in self.checks if not c.ok]

    @property
    def ok(self) -> bool:
        return bool(self.checks) and not self.failed

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "ok": self.ok,
            "steps_count": self.steps_count,
            "checks": [c.as_dict() for c in self.checks],
            "checks_passed": self.passed,
            "checks_failed": self.failed,
            "manual_interventions": self.manual_interventions,
            "commands": [c.as_dict() for c in self.commands],
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


def _json_payload(stdout: str) -> dict | None:
    """Last JSON object on stdout (product_flow_benchmark.py lines 1037-1045)."""
    for line in reversed([line.strip() for line in stdout.splitlines() if line.strip()]):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


class JourneyRunner:
    """Drives ``python -m exomem ...`` subprocesses against one isolated vault."""

    def __init__(self, workdir: Path):
        self.workdir = Path(workdir)
        self.vault = self.workdir / "vault"
        self.vault.mkdir(parents=True, exist_ok=True)
        self.env = journey_env(self.vault, self.workdir)
        self.commands: list[CommandRun] = []
        self.steps_count = 0
        real_home = Path(os.path.expanduser("~"))
        forbidden = {real_home, real_home / ".claude", real_home / ".codex"}
        if self.vault in forbidden:
            raise AssertionError("journey vault must never be a real client home")

    def run(self, *args: str) -> tuple[CommandRun, dict | None]:
        argv = [sys.executable, "-m", "exomem", *args]
        t0 = time.perf_counter()
        proc = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        run = CommandRun(
            argv=argv,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            seconds=time.perf_counter() - t0,
        )
        self.commands.append(run)
        self.steps_count += 1
        return run, _json_payload(proc.stdout)

    def init_vault(self) -> CommandRun:
        run, _ = self.run("init", "--vault", str(self.vault))
        return run

    def note_pages(self) -> list[str]:
        """Non-index note pages currently in the vault (page-count discipline)."""
        notes_root = self.vault / KB_DIRNAME / "Notes"
        return sorted(
            p.relative_to(self.vault).as_posix()
            for p in notes_root.rglob("*.md")
            if p.name != "index.md"
        )


def _data(payload: dict | None) -> dict | list:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return {}
    return payload.get("data") or {}


def _envelope_ok(payload: dict | None) -> bool:
    return isinstance(payload, dict) and payload.get("success") is True


def _hit_paths(payload: dict | None) -> list[str]:
    data = _data(payload)
    if not isinstance(data, list):
        return []
    return [hit.get("path", "") for hit in data if isinstance(hit, dict)]


# --------------------------------------------------------------------------- J1


def run_j1_longitudinal(
    workdir: Path, *, skip_final_replace: bool = False
) -> JourneyResult:
    """J1: longitudinal evolution of one research-note conclusion.

    remember v1 -> replace to v2 -> replace to v3 -> evolution shows the
    ordered 3-state chain -> current ask returns the v3-value page ->
    superseded pages remain readable -> no duplicate page sprawl.

    ``skip_final_replace`` is the deliberate wrong-order variant used by the
    tests to prove the checks bite (the chain check must then fail).
    """
    started = time.perf_counter()
    runner = JourneyRunner(workdir)
    checks: list[JourneyCheck] = []

    init = runner.init_vault()
    checks.append(JourneyCheck("init vault", init.ok, f"exit={init.returncode}"))

    def note_body(version: str, days: str, extra_unit: str) -> str:
        return (
            f"# Auth token rotation conclusion {version}\n\n"
            "## Question\n\n"
            "What rotation interval should auth tokens use?\n\n"
            "## Findings\n\n"
            f"Auth tokens rotate every {days} days per the {version} assessment.\n\n"
            "## Observations\n\n"
            f"- [auth] Auth tokens rotate every {days} days {extra_unit}\n"
        )

    v1_run, v1_payload = runner.run(
        "remember",
        "--title",
        "Auth token rotation conclusion",
        "--content",
        note_body("v1", "30", "#auth-tokens"),
        "--field",
        "note_type=research-note",
        "--field",
        "project=benchmark",
        "--json",
    )
    v1_path = str(_data(v1_payload).get("path", "")) if isinstance(_data(v1_payload), dict) else ""
    checks.append(
        JourneyCheck("remember v1 commits", v1_run.ok and bool(v1_path), v1_path or v1_run.stderr[-300:])
    )

    def replace(old_path: str, version: str, days: str) -> str:
        run, payload = runner.run(
            "replace_memory",
            old_path,
            "--title",
            f"Auth token rotation conclusion {version}",
            "--content",
            note_body(version, days, "#auth-tokens"),
            "--field",
            "note_type=research-note",
            "--field",
            "project=benchmark",
            "--field",
            f"reason=interval revised to {days} days",
            "--json",
        )
        data = _data(payload)
        paths = data.get("paths", []) if isinstance(data, dict) else []
        new_path = str(paths[1]) if len(paths) == 2 else ""
        checks.append(
            JourneyCheck(
                f"replace to {version} commits",
                run.ok and bool(new_path),
                new_path or run.stderr[-300:],
            )
        )
        return new_path

    v2_path = replace(v1_path, "v2", "60") if v1_path else ""
    v3_path = ""
    if v2_path and not skip_final_replace:
        v3_path = replace(v2_path, "v3", "90")
    expected_chain = [p for p in (v1_path, v2_path, v3_path) if p]
    current_path = expected_chain[-1] if expected_chain else ""
    current_value = "90" if not skip_final_replace else "60"

    evo_run, evo_payload = runner.run(
        "review_memory", "--mode", "evolution", "--path", v1_path, "--json"
    )
    evo_data = _data(evo_payload)
    timelines = evo_data.get("timelines", []) if isinstance(evo_data, dict) else []
    timeline = timelines[0] if timelines else {}
    versions = timeline.get("versions", [])
    version_paths = [row.get("path") for row in versions]
    statuses = [row.get("status") for row in versions]
    checks.append(
        JourneyCheck(
            "evolution shows 3-state chain in order",
            evo_run.ok
            and timeline.get("span", {}).get("n_versions") == 3
            and version_paths == [v1_path, v2_path, v3_path]
            and statuses == ["superseded", "superseded", "active"],
            f"n={timeline.get('span', {}).get('n_versions')} paths={version_paths} statuses={statuses}",
        )
    )
    checks.append(
        JourneyCheck(
            "evolution anchors: chain_id=newest, topic_anchor=oldest",
            timeline.get("chain_id") == current_path
            and timeline.get("topic_anchor") == v1_path,
            f"chain_id={timeline.get('chain_id')} topic_anchor={timeline.get('topic_anchor')}",
        )
    )

    ask_run, ask_payload = runner.run(
        "ask_memory", "auth token rotation interval", "--limit", "5", "--json"
    )
    hit_paths = _hit_paths(ask_payload)
    checks.append(
        JourneyCheck(
            "ask returns the current (v3-value) page first",
            ask_run.ok and bool(hit_paths) and hit_paths[0] == current_path == v3_path,
            f"top={hit_paths[:2]} expected={v3_path or '(missing v3)'}",
        )
    )
    top_body = ""
    if hit_paths:
        try:
            top_body = (runner.vault / hit_paths[0]).read_text(encoding="utf-8")
        except OSError:
            top_body = ""
    checks.append(
        JourneyCheck(
            "top hit carries the current value",
            f"every {current_value} days" in top_body and "per the v3 assessment" in top_body,
            f"value={current_value} in top-hit body: {current_value in top_body}",
        )
    )

    for label, superseded_path, marker in (
        ("v1", v1_path, "per the v1 assessment"),
        ("v2", v2_path, "per the v2 assessment"),
    ):
        read_run, read_payload = runner.run("read_memory", superseded_path, "--json")
        data = _data(read_payload)
        frontmatter = data.get("frontmatter", {}) if isinstance(data, dict) else {}
        body = data.get("body", "") if isinstance(data, dict) else ""
        checks.append(
            JourneyCheck(
                f"superseded {label} remains readable",
                read_run.ok
                and frontmatter.get("status") == "superseded"
                and marker in body,
                f"status={frontmatter.get('status')}",
            )
        )

    pages = runner.note_pages()
    expected_pages = 3 if not skip_final_replace else 2
    checks.append(
        JourneyCheck(
            "page-count discipline (no duplicate sprawl)",
            len(pages) == expected_pages and set(expected_chain) == set(pages),
            f"{len(pages)} pages: {pages}",
        )
    )

    return JourneyResult(
        id="j1_longitudinal" if not skip_final_replace else "j1_longitudinal_skip_replace",
        name="J1 longitudinal evolution",
        steps_count=runner.steps_count,
        checks=checks,
        commands=runner.commands,
        manual_interventions=0,
        elapsed_seconds=time.perf_counter() - started,
    )


# --------------------------------------------------------------------------- J2


J2_PARAPHRASES = (
    # All stems drawn from the correction note's own vocabulary (lexical
    # profile: hybrid retention needs every query stem in the page).
    "edge cache TTL",
    "edge cache TTL minutes",
    "cache TTL deploy tier",
    "deploy tier cache TTL minutes",
    "TTL minutes edge cache deploy",
)


def run_j2_correction(workdir: Path) -> JourneyResult:
    """J2: correction propagation with provenance.

    capture_source (wrong fact) -> remember conclusion citing it ->
    replace_memory corrected -> 5 ask paraphrases rank the corrected page
    above the superseded one (product prefer-active default; no extra flags)
    -> audit reports no contradiction between them -> corrected page retains
    the source citation in ``sources:`` frontmatter.
    """
    started = time.perf_counter()
    runner = JourneyRunner(workdir)
    checks: list[JourneyCheck] = []

    init = runner.init_vault()
    checks.append(JourneyCheck("init vault", init.ok, f"exit={init.returncode}"))

    cap_run, cap_payload = runner.run(
        "capture_source",
        "--content",
        "Deploy checklist: the edge cache TTL is 15 minutes for the deploy tier.",
        "--source-type",
        "session",
        "--title",
        "Edge cache TTL checklist",
        "--why-captured",
        "membench J2 correction journey",
        "--json",
    )
    cap_data = _data(cap_payload)
    source_path = str(cap_data.get("path", "")) if isinstance(cap_data, dict) else ""
    checks.append(
        JourneyCheck(
            "capture_source (wrong fact) commits",
            cap_run.ok and bool(source_path),
            source_path or cap_run.stderr[-300:],
        )
    )

    wrong_run, wrong_payload = runner.run(
        "remember",
        "--title",
        "Edge cache TTL conclusion",
        "--content",
        (
            "# Edge cache TTL conclusion\n\n"
            "## Claim\n\n"
            "The edge cache TTL is 15 minutes for the deploy tier.\n\n"
            "## Observations\n\n"
            "- [cache] The edge cache TTL is 15 minutes for the deploy tier #edge-cache\n"
        ),
        "--field",
        "note_type=insight",
        "--field",
        f"sources={source_path}",
        "--json",
    )
    wrong_data = _data(wrong_payload)
    wrong_path = str(wrong_data.get("path", "")) if isinstance(wrong_data, dict) else ""
    checks.append(
        JourneyCheck(
            "remember conclusion citing the source commits",
            wrong_run.ok and bool(wrong_path),
            wrong_path or wrong_run.stderr[-300:],
        )
    )

    fix_run, fix_payload = runner.run(
        "replace_memory",
        wrong_path,
        "--title",
        "Edge cache TTL correction",
        "--content",
        (
            "# Edge cache TTL correction\n\n"
            "## Claim\n\n"
            "The edge cache TTL is 30 minutes for the deploy tier after the correction.\n\n"
            "## Observations\n\n"
            "- [cache] The edge cache TTL is 30 minutes for the deploy tier #edge-cache\n"
        ),
        "--field",
        "note_type=insight",
        "--field",
        f"sources={source_path}",
        "--field",
        "reason=TTL corrected from 15 to 30 minutes",
        "--json",
    )
    fix_data = _data(fix_payload)
    fix_paths = fix_data.get("paths", []) if isinstance(fix_data, dict) else []
    corrected_path = str(fix_paths[1]) if len(fix_paths) == 2 else ""
    checks.append(
        JourneyCheck(
            "replace_memory corrected commits",
            fix_run.ok and bool(corrected_path),
            corrected_path or fix_run.stderr[-300:],
        )
    )

    for index, paraphrase in enumerate(J2_PARAPHRASES, start=1):
        ask_run, ask_payload = runner.run("ask_memory", paraphrase, "--limit", "5", "--json")
        paths = _hit_paths(ask_payload)
        corrected_rank = paths.index(corrected_path) if corrected_path in paths else -1
        wrong_rank = paths.index(wrong_path) if wrong_path in paths else -1
        checks.append(
            JourneyCheck(
                f"paraphrase {index} ranks corrected above superseded",
                ask_run.ok
                and corrected_rank >= 0
                and wrong_rank >= 0
                and corrected_rank < wrong_rank,
                f"{paraphrase!r}: corrected@{corrected_rank} superseded@{wrong_rank} in {paths}",
            )
        )

    audit_run, audit_payload = runner.run("maintain_memory", "--mode", "audit", "--json")
    audit_data = _data(audit_payload)
    findings = audit_data.get("findings", []) if isinstance(audit_data, dict) else []
    contradiction_rows = [
        row
        for row in findings
        if isinstance(row, dict)
        and "contradiction" in str(row.get("category", "")).lower()
        and (
            str(row.get("path", "")) in {corrected_path, wrong_path}
            or corrected_path in json.dumps(row)
            or wrong_path in json.dumps(row)
        )
    ]
    checks.append(
        JourneyCheck(
            "audit reports no contradiction between corrected and superseded",
            audit_run.ok and _envelope_ok(audit_payload) and not contradiction_rows,
            f"categories={sorted({str(r.get('category')) for r in findings if isinstance(r, dict)})}",
        )
    )

    read_run, read_payload = runner.run("read_memory", corrected_path, "--json")
    read_data = _data(read_payload)
    frontmatter = read_data.get("frontmatter", {}) if isinstance(read_data, dict) else {}
    sources_value = frontmatter.get("sources") or []
    # sources: frontmatter stores wikilinks without the .md suffix (probed).
    source_ref = source_path.removesuffix(".md")
    cites = any(source_ref in str(item) for item in sources_value)
    checks.append(
        JourneyCheck(
            "corrected page retains source provenance in sources: frontmatter",
            read_run.ok and frontmatter.get("status") == "active" and cites,
            f"sources={sources_value}",
        )
    )

    wrong_read_run, wrong_read_payload = runner.run("read_memory", wrong_path, "--json")
    wrong_frontmatter = (
        _data(wrong_read_payload).get("frontmatter", {})
        if isinstance(_data(wrong_read_payload), dict)
        else {}
    )
    checks.append(
        JourneyCheck(
            "superseded conclusion remains readable and marked",
            wrong_read_run.ok and wrong_frontmatter.get("status") == "superseded",
            f"status={wrong_frontmatter.get('status')}",
        )
    )

    return JourneyResult(
        id="j2_correction",
        name="J2 correction propagation",
        steps_count=runner.steps_count,
        checks=checks,
        commands=runner.commands,
        manual_interventions=0,
        elapsed_seconds=time.perf_counter() - started,
    )
