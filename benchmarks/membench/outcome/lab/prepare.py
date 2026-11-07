"""Prepare Harvey LAB matters for the multi-document synthesis suite.

LAB (MIT) ships synthetic matters: a folder of source documents, one task, and
a rubric of ``match_criteria``. Its own grader needs paid API keys, so this
suite plays only the agent side and judges with its own blind judge against the
same criteria.

``setup`` builds LAB's own environment and pins its parser. ``sample`` draws
the dev and held-out task ids. ``convert`` turns a task's documents into
Markdown text with LAB's own ``parse_doc.py``, and copies the task and rubric
into a prepared tree that ``run`` and ``judge`` read, so neither touches the
LAB checkout.

Usage (from ``/var/tmp`` with ``PYTHONPATH=benchmarks:src``)::

    python -m membench.outcome.lab.prepare setup
    python -m membench.outcome.lab.prepare sample
    python -m membench.outcome.lab.prepare convert --run-id prep-dev --cases dev
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import random
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

RUN_ROOT = Path(os.environ.get("OUTCOME_EVAL_ROOT", "/var/tmp/exomem-outcome-evals"))
UPSTREAM = RUN_ROOT / "upstream" / "harvey-labs"
UPSTREAM_COMMIT = "ba363f57c900166b616062c8153560d0508d9175"
TASKS = UPSTREAM / "tasks"
PARSE_DOC = UPSTREAM / "lab_core" / "sandbox" / "parsers" / "parse_doc.py"
LAB_PYTHON = UPSTREAM / ".venv" / "bin" / "python"
PANDOC_PREFIX = RUN_ROOT / "tools" / "pandoc"
CASES = RUN_ROOT / "cases" / "lab"

SEED = 20261108
DEV_SIZE, HELDOUT_SIZE = 3, 20
#: A matter needs enough documents to be a synthesis task. The upper bound keeps data rooms out:
#: every diligence task holds 2600 or more documents, which no 30-minute compile session can capture.
MIN_DOCUMENTS, MAX_DOCUMENTS = 8, 40

#: The formats ``parse_doc.py`` converts (its own ``PARSERS`` table); any other extension is plain text.
PARSED_FORMATS = ("docx", "pdf", "pptx", "xlsx")
PARSE_TIMEOUT_SECONDS = 300


def slug(task_id: str) -> str:
    return task_id.replace("/", "--")


# --------------------------------------------------------------------------
# Eligibility and the draw.
# --------------------------------------------------------------------------


def documents_of(task_dir: Path) -> list[Path]:
    return sorted(p for p in (task_dir / "documents").rglob("*") if p.is_file())


def exclusion(task_dir: Path, config: dict) -> str | None:
    """Why a task is not part of the pool, or None when it is."""
    if "docs_dir" in config:
        return "shares a corpus (docs_dir)"
    if "deliverables" not in config:
        return "declares no deliverable"
    if len(config["deliverables"]) != 1:
        return "declares more than one deliverable"
    count = len(documents_of(task_dir)) if (task_dir / "documents").is_dir() else 0
    if count < MIN_DOCUMENTS:
        return f"fewer than {MIN_DOCUMENTS} documents"
    if count > MAX_DOCUMENTS:
        return f"more than {MAX_DOCUMENTS} documents"
    return None


def survey() -> tuple[dict[str, list[str]], Counter, list[str]]:
    """Eligible task ids by area, the tasks each exclusion removed, and the areas left empty."""
    eligible: dict[str, list[str]] = {}
    excluded: Counter = Counter()
    areas: set[str] = set()
    for path in sorted(TASKS.rglob("task.json")):
        task_dir = path.parent
        task_id = task_dir.relative_to(TASKS).as_posix()
        areas.add(task_id.split("/", 1)[0])
        reason = exclusion(task_dir, json.loads(path.read_text(encoding="utf-8")))
        if reason:
            excluded[reason] += 1
        else:
            eligible.setdefault(task_id.split("/", 1)[0], []).append(task_id)
    return eligible, excluded, sorted(areas - set(eligible))


def draw(eligible: dict[str, list[str]], rng: random.Random) -> tuple[list[str], list[str]]:
    """One task per area, areas in shuffled order: the first go to dev, the next to held-out."""
    areas = sorted(eligible)
    needed = DEV_SIZE + HELDOUT_SIZE
    if len(areas) < needed:
        raise SystemExit(f"{len(areas)} areas cannot give {needed} tasks one per area")
    rng.shuffle(areas)
    picks = [rng.choice(sorted(eligible[area])) for area in areas[:needed]]
    return picks[:DEV_SIZE], picks[DEV_SIZE:]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sample() -> None:
    CASES.mkdir(parents=True, exist_ok=True)
    targets = {name: CASES / f"{name}.json" for name in ("dev", "heldout")}
    for target in (*targets.values(), CASES / "README.md"):
        if target.exists():
            raise SystemExit(f"{target} exists; the draw is fixed once written")
    eligible, excluded, empty_areas = survey()
    dev, heldout = draw(eligible, random.Random(SEED))
    targets["dev"].write_text(json.dumps(dev, indent=2) + "\n")
    targets["heldout"].write_text(json.dumps(heldout, indent=2) + "\n")
    drawn = Counter(t.split("/", 1)[0] for t in dev + heldout)
    rows = [
        f"| {area} | {len(ids)} | {drawn.get(area, 0)} |" for area, ids in sorted(eligible.items())
    ]
    exclusions = [f"- {reason}: {count}" for reason, count in sorted(excluded.items())]
    readme = f"""# Harvey LAB multi-document synthesis cases

Pinned upstream: harveyai/harvey-labs at `{UPSTREAM_COMMIT}` (MIT).

A task is eligible when it has its own `documents/` folder with {MIN_DOCUMENTS} to
{MAX_DOCUMENTS} files and declares exactly one deliverable. The practice area is the first path
segment under `tasks/`. Eligible tasks: {sum(len(v) for v in eligible.values())} in {len(eligible)} areas.
Tasks removed by the filter (a task counts under its first failing test):

{chr(10).join(exclusions)}

Areas with no eligible task: {", ".join(empty_areas)}.

The draw uses `random.Random({SEED})`: one task per area, areas in shuffled order, the first
{DEV_SIZE} areas give the dev set and the next {HELDOUT_SIZE} the held-out set. The sets are disjoint and
share no area.

```python
{inspect.getsource(draw)}```

| area | eligible tasks | drawn |
| --- | ---: | ---: |
{chr(10).join(rows)}

| file | tasks | sha256 |
| --- | ---: | --- |
| `dev.json` | {len(dev)} | `{sha256_of(targets["dev"])}` |
| `heldout.json` | {len(heldout)} | `{sha256_of(targets["heldout"])}` |
"""
    (CASES / "README.md").write_text(readme)
    print(f"dev={len(dev)} heldout={len(heldout)} areas={len(eligible)} -> {CASES}")


# --------------------------------------------------------------------------
# LAB's own environment.
# --------------------------------------------------------------------------


def setup() -> None:
    """LAB's venv from its own lock file, and the pandoc release its sandbox image pins."""
    head = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if head != UPSTREAM_COMMIT:
        raise SystemExit(f"{UPSTREAM} is at {head}, not the pinned {UPSTREAM_COMMIT}")
    subprocess.run(  # noqa: S603 - fixed argv
        ["uv", "sync", "--frozen", "--no-dev", "--project", str(UPSTREAM)], check=True
    )
    if not (PANDOC_PREFIX / "bin" / "pandoc").exists():
        # LAB's installer checks the download against the release's published SHA-256.
        installer = UPSTREAM / "lab_core" / "sandbox" / "install_pandoc.sh"
        subprocess.run(["sh", str(installer), str(PANDOC_PREFIX)], check=True)  # noqa: S603


# --------------------------------------------------------------------------
# Conversion.
# --------------------------------------------------------------------------


def parser_env(scratch: Path) -> dict[str, str]:
    """A parser is handed untrusted files: no inherited environment, only pandoc on PATH."""
    return {
        "PATH": f"{PANDOC_PREFIX / 'bin'}:/usr/bin:/bin",
        "HOME": str(scratch),
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def to_text(path: Path, scratch: Path) -> tuple[str, str]:
    """``(text, route)`` for one source document, converted the way LAB's own agent reads it."""
    fmt = path.suffix.lower().lstrip(".")
    if fmt in PARSED_FORMATS:
        # Own process, isolated mode, the path as an argument, and a cwd that is not the document folder.
        proc = subprocess.run(  # noqa: S603 - fixed argv
            [str(LAB_PYTHON), "-I", str(PARSE_DOC), fmt, str(path)],
            cwd=scratch,
            env=parser_env(scratch),
            capture_output=True,
            timeout=PARSE_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"parse_doc exit {proc.returncode}: {proc.stderr.decode()[-300:]}")
        return proc.stdout.decode("utf-8"), "parse_doc"
    return path.read_bytes().decode("utf-8"), "plain"


def convert_task(task_id: str, out: Path, scratch: Path) -> dict:
    task_dir = TASKS / task_id
    config = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    reason = exclusion(task_dir, config)
    if reason:
        raise SystemExit(f"{task_id} is not an eligible task: {reason}")
    ((deliverable, _),) = config["deliverables"].items()
    records = []
    for source in documents_of(task_dir):
        rel = source.relative_to(task_dir / "documents")
        target = out / "documents" / f"{rel.as_posix()}.md"
        record = {
            "source": rel.as_posix(),
            "sha256": sha256_of(source),
            "bytes": source.stat().st_size,
        }
        start = time.monotonic()
        try:
            text, record["route"] = to_text(source, scratch)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            record.update(converted=target.relative_to(out).as_posix(), chars=len(text))
            record["empty"] = not text.strip()
        except Exception as exc:  # noqa: BLE001 - a failed document is recorded, never skipped silently
            record["fault"] = f"{type(exc).__name__}: {exc}"
        record["seconds"] = round(time.monotonic() - start, 2)
        records.append(record)
    case = {
        "task_id": task_id,
        "area": task_id.split("/", 1)[0],
        "title": config["title"],
        "instructions": config["instructions"],
        # The deliverable's stem: arms write it as Markdown, whatever format LAB asked for.
        "deliverable": Path(deliverable).stem,
        "deliverable_declared": deliverable,
        "criteria": [
            {"id": c["id"], "title": c["title"], "match_criteria": c["match_criteria"]}
            for c in config["criteria"]
        ],
        "documents": records,
    }
    (out / "case.json").write_text(json.dumps(case, indent=2))
    return case


def convert(run_id: str, task_ids: list[str]) -> Path:
    prepared = RUN_ROOT / "runs" / run_id
    if prepared.exists():
        raise SystemExit(f"{prepared} exists; choose a new --run-id")
    scratch = prepared / "parser-scratch"
    scratch.mkdir(parents=True)
    manifest = {
        "run_id": run_id,
        "upstream_commit": UPSTREAM_COMMIT,
        "parser": str(PARSE_DOC.relative_to(UPSTREAM)),
        "pandoc": subprocess.run(  # noqa: S603 - fixed argv
            [str(PANDOC_PREFIX / "bin" / "pandoc"), "--version"], capture_output=True, text=True
        ).stdout.splitlines()[0],
        "tasks": task_ids,
        "faults": [],
    }
    for task_id in task_ids:
        case = convert_task(task_id, prepared / "tasks" / slug(task_id), scratch)
        faults = [d for d in case["documents"] if "fault" in d]
        empty = [d["source"] for d in case["documents"] if d.get("empty")]
        manifest["faults"] += [
            {"task": task_id, "source": d["source"], "fault": d["fault"]} for d in faults
        ]
        print(
            f"{task_id}: {len(case['documents'])} documents, {len(faults)} faults, {len(empty)} empty",
            flush=True,
        )
    shutil.rmtree(scratch)
    (prepared / "manifest.json").write_text(json.dumps(manifest, indent=2))
    if manifest["faults"]:
        raise SystemExit(
            f"{len(manifest['faults'])} documents failed to convert; see {prepared}/manifest.json"
        )
    return prepared


def task_ids_from(cases: str | None, tasks: str | None) -> list[str]:
    if tasks:
        return tasks.split(",")
    path = Path(cases) if cases and "/" in cases else CASES / f"{cases}.json"
    return json.loads(path.read_text())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="build LAB's venv and install its pinned pandoc")
    sub.add_parser("sample", help="draw the dev and held-out task ids")
    p_convert = sub.add_parser("convert", help="convert the documents of selected tasks")
    p_convert.add_argument("--run-id", required=True)
    p_convert.add_argument("--cases", help="`dev`, `heldout` or a path to a JSON list of task ids")
    p_convert.add_argument("--tasks", help="comma-separated task ids, instead of --cases")
    args = parser.parse_args(argv)
    if args.cmd == "setup":
        setup()
    elif args.cmd == "sample":
        sample()
    else:
        if not (args.cases or args.tasks):
            parser.error("give --cases or --tasks")
        print(convert(args.run_id, task_ids_from(args.cases, args.tasks)))


if __name__ == "__main__":
    sys.exit(main())
