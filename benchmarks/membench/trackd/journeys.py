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


# --------------------------------------------------------------------------- J3

J3_RUBRIC_PATH = (
    Path(__file__).resolve().parent / "rubrics" / "j3_weekly_review.rubric.json"
)


@dataclass(frozen=True)
class PlantedItem:
    """One deliberately planted review item the queues must surface."""

    plant_id: str
    kind: str  # stale | contradiction | unprocessed | open_loop
    path: str

    def as_dict(self) -> dict:
        return {"plant_id": self.plant_id, "kind": self.kind, "path": self.path}


@dataclass(frozen=True)
class QueueObservation:
    """What one ``review_memory`` mode actually surfaced."""

    mode: str
    paths: tuple[str, ...]
    supported: bool = True


def score_review_queue(
    planted: list[PlantedItem] | tuple[PlantedItem, ...],
    observation: QueueObservation,
    *,
    expected_kinds: tuple[str, ...],
) -> dict:
    """Deterministic planted-id recall/precision + false-surface rate.

    An unsupported queue (e.g. the contradiction sweep under the lexical
    profile, which is embeddings-gated) reports every metric as ``None`` —
    unsupported is NEVER converted to a zero. ``precision`` and
    ``false_surface_rate`` are ``None`` when nothing surfaced (no claim can
    be made about an empty list's composition).
    """

    expected = sorted({p.path for p in planted if p.kind in expected_kinds})
    surfaced_set = set(observation.paths)
    base = {
        "mode": observation.mode,
        "supported": observation.supported,
        "expected": expected,
        "surfaced": list(observation.paths),
    }
    if not observation.supported:
        return {
            **base,
            "matched": [],
            "recall": None,
            "precision": None,
            "false_surfaces": [],
            "false_surface_rate": None,
        }
    matched = sorted(surfaced_set & set(expected))
    false_surfaces = sorted(surfaced_set - set(expected))
    return {
        **base,
        "matched": matched,
        "recall": (len(matched) / len(expected)) if expected else None,
        "precision": (len(matched) / len(surfaced_set)) if surfaced_set else None,
        "false_surfaces": false_surfaces,
        "false_surface_rate": (
            len(false_surfaces) / len(surfaced_set) if surfaced_set else None
        ),
    }


def load_j3_rubric(path: Path = J3_RUBRIC_PATH) -> dict:
    """Load and validate the J3 blind-pairwise rubric JSON."""

    rubric = json.loads(Path(path).read_text(encoding="utf-8"))
    if rubric.get("journey") != "j3_weekly_review":
        raise ValueError("rubric journey must be j3_weekly_review")
    if rubric.get("pairing") != "blind" or rubric.get("order") != "randomized":
        raise ValueError("rubric must declare pairing=blind and order=randomized")
    samples = rubric.get("samples")
    if not isinstance(samples, int) or samples < 1:
        raise ValueError("rubric samples must be an int >= 1")
    criteria = rubric.get("criteria")
    if not criteria:
        raise ValueError("rubric criteria must be non-empty")
    for criterion in criteria:
        if not criterion.get("id") or not criterion.get("question"):
            raise ValueError("every criterion needs id and question")
        anchors = criterion.get("anchors") or {}
        if set(anchors) != {"1", "2", "3", "4", "5"} or not all(
            isinstance(v, str) and v for v in anchors.values()
        ):
            raise ValueError(
                f"criterion {criterion.get('id')!r} needs complete 1..5 anchors"
            )
    return rubric


def write_j3_judge_requests(
    run_dir: Path,
    summaries: dict[str, str],
    *,
    rubric_path: Path = J3_RUBRIC_PATH,
    seed: str = "membench-j3",
) -> Path:
    """Route J3 summaries through the existing blind judge handshake.

    Reuses :mod:`membench.judge` wholesale: provider identities become
    deterministic ``system-X`` tokens, summaries are normalized for the judge,
    line order is the writer's seed-derived permutation (the rubric's
    ``order: randomized``), each item expands to the rubric's ``samples``,
    and the fail-closed leakage gate applies to every serialized line.
    """

    from membench.judge import BlindingMap, RequestItem, write_requests
    from membench.judge.blinding import normalize_for_judge

    rubric = load_j3_rubric(rubric_path)
    blinding = BlindingMap.mint(sorted(summaries), f"{seed}:providers")
    items: list[RequestItem] = []
    for criterion in rubric["criteria"]:
        for provider in sorted(summaries):
            token = blinding.token_for(provider)
            summary = normalize_for_judge(summaries[provider])
            anchor_lines = "\n".join(
                f"{score}: {criterion['anchors'][score]}" for score in ("1", "2", "3", "4", "5")
            )
            prompt = (
                "You are grading one anonymized weekly-review summary from a "
                "memory system. System identities are blinded; grade only what "
                "is written.\n\n"
                f"Criterion: {criterion['question']}\n\n"
                f"Anchors:\n{anchor_lines}\n\n"
                f"Summary under review:\n{summary}\n\n"
                "Reply with STRICT JSON only - one object, no prose:\n"
                '{"score": 1-5, "reason": "short reason"}\n'
            )
            items.append(
                RequestItem(
                    item_id=f"j3:{criterion['id']}:{token}",
                    blinded_provider_token=token,
                    payload={
                        "task": "judge",
                        "journey": "j3_weekly_review",
                        "criterion_id": criterion["id"],
                        "question": criterion["question"],
                        "anchors": criterion["anchors"],
                        "summary": summary,
                        "prompt": prompt,
                    },
                )
            )
    return write_requests(
        run_dir, "judge", items, samples=rubric["samples"], seed=seed
    )


@dataclass
class J3Result(JourneyResult):
    """J1/J2-shaped result plus the planted registry, queue scores, and the
    judge-facing summary text."""

    planted: list[PlantedItem] = field(default_factory=list)
    queue_scores: list[dict] = field(default_factory=list)
    summary_text: str = ""

    def as_dict(self) -> dict:
        payload = super().as_dict()
        payload["planted"] = [p.as_dict() for p in self.planted]
        payload["queue_scores"] = self.queue_scores
        payload["summary_text"] = self.summary_text
        return payload


def _queue_paths(payload: dict | None) -> tuple[str, ...]:
    data = _data(payload)
    items = data.get("items", []) if isinstance(data, dict) else []
    return tuple(
        str(item.get("path", ""))
        for item in items
        if isinstance(item, dict) and item.get("path")
    )


def run_j3_weekly_review(workdir: Path) -> J3Result:
    """J3: weekly review over planted stale/contradiction/unprocessed/open-loop
    items, scored by planted-id recall+precision, false-surface rate, and
    triage burden (scripted op count).

    Product-contract notes (probed by RUNNING the CLI, 2026-08-01, this
    worktree):

    - Wall-clock staleness CANNOT be planted through public write surfaces:
      ``remember`` rejects ``created``/``updated`` fields (UNKNOWN_PARAM) and
      ``edit_memory`` always re-bumps ``updated:`` to today. The journey
      therefore collapses the age edge with the product's documented gate-edge
      knob ``EXOMEM_STALE_AGE_DAYS=0`` (recorded in the journey env), so the
      stale queue measures the DORMANCY conjunct: an unlinked conclusion is
      flagged, well-linked decoys (>= 2 inbound wikilinks) are not.
    - The corpus-contradiction sweep is embeddings-gated and no-ops under
      ``EXOMEM_DISABLE_EMBEDDINGS`` (the deterministic lexical profile), so
      the planted contradiction pair is recorded and the queue is scored
      UNSUPPORTED — never zero.
    - Compiled pages after the first need a qualifying typed relation
      (``## Relations`` bullets; ``sources=`` frontmatter maps to the excluded
      ``derivation`` family and does NOT qualify). The first note commits under
      the automatic bootstrap disposition and doubles as the planted OPEN-LOOP
      item: it surfaces as ``relation_debt`` in the attention queue.
    - A note citing a captured source via ``sources=`` back-fills the source's
      ``ingested_into``, which is what keeps the decoy capture OUT of the
      unprocessed-sources queue.
    """

    started = time.perf_counter()
    runner = JourneyRunner(workdir)
    # Declared product config: collapse the stale age edge (see docstring).
    runner.env["EXOMEM_STALE_AGE_DAYS"] = "0"
    checks: list[JourneyCheck] = []
    planted: list[PlantedItem] = []

    init = runner.init_vault()
    checks.append(JourneyCheck("init vault", init.ok, f"exit={init.returncode}"))

    def capture(title: str, content: str) -> str:
        run, payload = runner.run(
            "capture_source",
            "--content",
            content,
            "--source-type",
            "session",
            "--title",
            title,
            "--why-captured",
            "membench J3 weekly-review journey",
            "--json",
        )
        data = _data(payload)
        path = str(data.get("path", "")) if isinstance(data, dict) else ""
        checks.append(
            JourneyCheck(
                f"capture {title!r} commits", run.ok and bool(path), path or run.stderr[-300:]
            )
        )
        return path

    s1 = capture(
        "Pager duty scribble", "Raw scribble one: rotate the pager duty each sprint."
    )
    s2 = capture(
        "Deploy window scribble", "Raw scribble two: the deploy window opens at nine."
    )
    s3 = capture(
        "Session cap brief", "Compiled brief: the session cap policy needs review."
    )
    planted.append(PlantedItem("p-unprocessed-a", "unprocessed", s1))
    planted.append(PlantedItem("p-unprocessed-b", "unprocessed", s2))
    # s3 is the decoy: compiled below, so it must NOT surface as unprocessed.

    def remember(title: str, content: str, *fields: str) -> str:
        args = ["remember", "--title", title, "--content", content]
        for field_arg in fields:
            args.extend(["--field", field_arg])
        args.append("--json")
        run, payload = runner.run(*args)
        data = _data(payload)
        path = str(data.get("path", "")) if isinstance(data, dict) else ""
        checks.append(
            JourneyCheck(
                f"remember {title!r} commits", run.ok and bool(path), path or run.stderr[-300:]
            )
        )
        return path

    # N1 - first note: bootstrap relation disposition = the planted OPEN LOOP
    # (surfaces as relation_debt); also the well-linked stale-queue decoy.
    n1 = remember(
        "Deploy freeze conclusion",
        (
            "# Deploy freeze conclusion\n\n## Findings\n\n"
            "The deploy freeze window covers release week.\n\n"
            "## Observations\n\n"
            "- [deploy] The deploy freeze window covers release week #deploy-freeze\n"
        ),
        "note_type=research-note",
        "project=weekly-review",
    )
    n1_ref = n1.removesuffix(".md")
    planted.append(PlantedItem("p-open-loop", "open_loop", n1))

    # N2/N3 - the planted contradiction pair (conflicting session-cap values).
    n2 = remember(
        "Session cap conclusion",
        (
            "# Session cap conclusion\n\n## Claim\n\n"
            "The session cap is 20 concurrent sessions.\n\n"
            "## Observations\n\n"
            "- [limits] The session cap is 20 concurrent sessions #session-cap\n\n"
            f"## Relations\n\n- refines [[{n1_ref}]]\n"
        ),
        "note_type=insight",
        f"sources={s3}",
    )
    n2_ref = n2.removesuffix(".md")
    n3 = remember(
        "Session cap revision",
        (
            "# Session cap revision\n\n## Claim\n\n"
            "The session cap is 50 concurrent sessions.\n\n"
            f"Background: [[{n1_ref}]].\n\n"
            "## Observations\n\n"
            "- [limits] The session cap is 50 concurrent sessions #session-cap\n\n"
            f"## Relations\n\n- contradicts [[{n2_ref}]]\n"
        ),
        "note_type=insight",
        f"sources={s3}",
    )
    n3_ref = n3.removesuffix(".md")
    planted.append(PlantedItem("p-contradiction-a", "contradiction", n2))
    planted.append(PlantedItem("p-contradiction-b", "contradiction", n3))

    # N4 - the planted DORMANT conclusion: no inbound links (its outbound
    # links give N2/N3 inbound degree instead).
    n4 = remember(
        "Retry backoff conclusion",
        (
            "# Retry backoff conclusion\n\n## Findings\n\n"
            "Retries use exponential backoff with 3 attempts.\n\n"
            f"Caps context: [[{n2_ref}]] and [[{n3_ref}]].\n\n"
            "## Observations\n\n"
            "- [infra] Retries use exponential backoff with 3 attempts #retry\n\n"
            f"## Relations\n\n- refines [[{n1_ref}]]\n"
        ),
        "note_type=research-note",
        "project=weekly-review",
    )
    planted.append(PlantedItem("p-stale", "stale", n4))

    # H - production-log hub (outside the stale-review type set): links every
    # decoy note so each carries >= 2 inbound wikilinks.
    remember(
        "Weekly ops log",
        (
            f"# Weekly ops log\n\nReviewed [[{n1_ref}]], [[{n2_ref}]], "
            f"[[{n3_ref}]] this week.\n\n"
            "## Observations\n\n"
            "- [ops] Weekly ops review completed for deploy and session notes #ops-log\n\n"
            f"## Relations\n\n- supports [[{n1_ref}]]\n"
        ),
        "note_type=production-log",
        "medium=ops",
    )

    # ---- the weekly review itself: four queues -------------------------
    observations: dict[str, QueueObservation] = {}
    for mode in ("stale", "contradiction", "unprocessed-sources", "attention"):
        run, payload = runner.run("review_memory", "--mode", mode, "--json")
        checks.append(
            JourneyCheck(
                f"review_memory mode={mode} responds",
                run.ok and _envelope_ok(payload),
                f"exit={run.returncode}",
            )
        )
        observations[mode] = QueueObservation(
            mode=mode,
            paths=_queue_paths(payload),
            # The contradiction sweep is embeddings-gated: honestly
            # unsupported under the lexical profile, never scored zero.
            supported=(mode != "contradiction"),
        )

    stale_score = score_review_queue(
        planted, observations["stale"], expected_kinds=("stale",)
    )
    contradiction_score = score_review_queue(
        planted, observations["contradiction"], expected_kinds=("contradiction",)
    )
    unprocessed_score = score_review_queue(
        planted, observations["unprocessed-sources"], expected_kinds=("unprocessed",)
    )
    # Attention = the open-loop union view of everything surfaceable in this
    # profile (contradictions cannot surface here; see docstring).
    attention_score = score_review_queue(
        planted,
        observations["attention"],
        expected_kinds=("stale", "unprocessed", "open_loop"),
    )
    queue_scores = [stale_score, contradiction_score, unprocessed_score, attention_score]

    checks.append(
        JourneyCheck(
            "stale queue surfaces exactly the planted dormant conclusion",
            stale_score["recall"] == 1.0 and stale_score["false_surface_rate"] == 0.0,
            f"recall={stale_score['recall']} false={stale_score['false_surfaces']}",
        )
    )
    checks.append(
        JourneyCheck(
            "unprocessed queue surfaces exactly the planted raw sources",
            unprocessed_score["recall"] == 1.0
            and unprocessed_score["false_surface_rate"] == 0.0,
            f"recall={unprocessed_score['recall']} false={unprocessed_score['false_surfaces']}",
        )
    )
    checks.append(
        JourneyCheck(
            "contradiction queue honestly unsupported in the lexical profile",
            contradiction_score["supported"] is False
            and contradiction_score["recall"] is None
            and not observations["contradiction"].paths,
            "embeddings-gated sweep; planted pair recorded, metrics None (never zero)",
        )
    )
    checks.append(
        JourneyCheck(
            "attention queue covers every surfaceable open loop",
            attention_score["recall"] == 1.0,
            f"recall={attention_score['recall']} "
            f"extra={attention_score['false_surfaces']}",
        )
    )
    burden = runner.steps_count
    checks.append(
        JourneyCheck(
            "triage burden equals the scripted op count",
            burden == 13,
            f"burden={burden} scripted ops, 0 manual interventions",
        )
    )

    def _fmt_rate(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}"

    summary_text = (
        "Weekly review sweep: the dormant-conclusion queue surfaced "
        f"{len(stale_score['matched'])} of {len(stale_score['expected'])} planted "
        f"items (false-surface rate {_fmt_rate(stale_score['false_surface_rate'])}); "
        "the raw-capture backlog queue surfaced "
        f"{len(unprocessed_score['matched'])} of {len(unprocessed_score['expected'])} "
        f"planted items (false-surface rate "
        f"{_fmt_rate(unprocessed_score['false_surface_rate'])}); "
        "the contradiction sweep is unsupported in this deterministic profile "
        "and is reported unsupported rather than zero; the combined attention "
        f"view covered {len(attention_score['matched'])} of "
        f"{len(attention_score['expected'])} open loops; triage burden was "
        f"{burden} scripted operations with 0 manual interventions."
    )

    return J3Result(
        id="j3_weekly_review",
        name="J3 weekly review",
        steps_count=runner.steps_count,
        checks=checks,
        commands=runner.commands,
        manual_interventions=0,
        elapsed_seconds=time.perf_counter() - started,
        planted=planted,
        queue_scores=queue_scores,
        summary_text=summary_text,
    )
