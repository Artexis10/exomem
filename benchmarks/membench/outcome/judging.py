"""One blind judge call, on the Claude or the Codex backend.

The lab and planted judges each send one prompt and need one structured verdict back.
Which model judges is a flag. Neither backend gets a tool, an MCP server or a memory
file, so a judge cannot read the arm key that sits beside its items.
"""

from __future__ import annotations

import json
import math
import random
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from epistemic.journeys.f27_replay import discover_agent_envelope, refuse_unsafe_out_dir

from membench.utility.switch_eval.judge import JudgeError, _refuse_tool_use, claude_argv, codex_argv

from .isolation import claude_env

#: A judge backend per model family; the choice is the operator's, the default model is the strongest one.
DEFAULT_MODELS = {"claude": "claude-opus-5-5", "codex": "gpt-6.1-sol"}
#: The Codex judge's reasoning effort. The Claude judge takes the CLI default, as before.
DEFAULT_EFFORT = {"claude": None, "codex": "high"}


@dataclass(frozen=True)
class JudgeSpec:
    backend: str
    model: str
    effort: str | None = None
    #: The CLI to run; None finds ``claude`` or ``codex`` on PATH.
    executable: str | None = None

    @classmethod
    def of(
        cls,
        backend: str,
        model: str | None = None,
        effort: str | None = None,
        executable: str | None = None,
    ) -> JudgeSpec:
        if backend not in DEFAULT_MODELS:
            raise SystemExit(f"--judge takes {sorted(DEFAULT_MODELS)}, not {backend!r}")
        return cls(
            backend, model or DEFAULT_MODELS[backend], effort or DEFAULT_EFFORT[backend], executable
        )

    def record(self) -> dict:
        """What a judgement file says about its judge."""
        return {"backend": self.backend, "model": self.model, "effort": self.effort}


@dataclass
class Judgement:
    verdict: dict | None
    cost_usd: float
    model_reported: list[str]
    error: str


class Judge:
    """Runs one spec's judge in an empty working directory under ``work``."""

    def __init__(self, spec: JudgeSpec, work: Path) -> None:
        self.spec = spec
        self.cwd = refuse_unsafe_out_dir(work / "cwd")
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.empty_mcp = work / "no-mcp.json"
        self.empty_mcp.write_text('{"mcpServers": {}}')
        self.schema_file = work / "verdict-schema.json"
        self.env = claude_env()[0]

    def argv(self, prompt: str, schema: dict, out: Path) -> list[str]:
        spec = self.spec
        if spec.backend == "claude":
            executable = spec.executable or str(discover_agent_envelope().executable)
            return claude_argv(
                executable=executable,
                model=spec.model,
                effort=spec.effort,
                schema=schema,
                prompt=prompt,
                mcp_config=self.empty_mcp,
            )
        self.schema_file.write_text(json.dumps(schema, indent=2))
        # The prompt goes in on stdin ("-"): a matter's deliverable can outgrow one argv string.
        argv = codex_argv(
            model=spec.model,
            effort=spec.effort or "",
            schema=self.schema_file,
            out=out,
            sandbox=self.cwd,
            prompt="-",
        )
        return [spec.executable, *argv[1:]] if spec.executable else argv

    def call(self, prompt: str, schema: dict, *, out: Path, timeout: float) -> Judgement:
        argv = self.argv(prompt, schema, out)
        codex = self.spec.backend == "codex"
        try:
            proc = subprocess.run(  # noqa: S603 - argv from the shared judge builders
                argv,
                input=prompt if codex else None,
                stdin=None if codex else subprocess.DEVNULL,
                cwd=self.cwd,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return Judgement(None, 0.0, [], f"timeout after {timeout} s")
        tail = f"{proc.stdout[-200:]} {proc.stderr[-200:]}"
        if codex:
            try:
                _refuse_tool_use(proc.stdout)
                verdict = json.loads(out.read_text(encoding="utf-8"))
            except (JudgeError, OSError, json.JSONDecodeError) as error:
                return Judgement(None, 0.0, [], f"{error}: {tail}")
            return Judgement(verdict, 0.0, [], "")
        try:
            result = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except json.JSONDecodeError:
            result = {}
        return Judgement(
            result.get("structured_output"),
            float(result.get("total_cost_usd") or 0.0),
            sorted(result.get("modelUsage") or {}),
            "" if result.get("structured_output") else tail,
        )


def grade_item(
    judge: Judge,
    item_id: str,
    prompt: str,
    schema: dict,
    *,
    out_dir: Path,
    timeout: float,
    problem: Callable[[dict], str | None],
    attempts: int = 3,
) -> dict:
    """One item's verdict row: retry until the verdict has the schema's shape and ``problem`` finds nothing wrong."""
    cost, errors, reported = 0.0, [], []
    for attempt in range(attempts):
        call = judge.call(
            prompt, schema, out=out_dir / f"{item_id}-{attempt}.codex-out.json", timeout=timeout
        )
        cost += call.cost_usd
        reported = call.model_reported or reported
        error = (
            (call.error or "no structured verdict")
            if call.verdict is None
            else problem(call.verdict)
        )
        if error is None:
            return {
                "id": item_id,
                "verdict": call.verdict,
                "judge_cost_usd": cost,
                "judge_model_reported": reported,
                "errors": errors,
            }
        errors.append(error)
    return {
        "id": item_id,
        "verdict": None,
        "judge_cost_usd": cost,
        "judge_model_reported": reported,
        "errors": errors,
    }


def judge_all(
    judge_dir: Path,
    spec: JudgeSpec,
    items: list[dict],
    grade: Callable[[dict, Judge], dict],
    *,
    rejudge_fraction: float,
    jobs: int,
    seed: int,
) -> None:
    """Judge every item once and a sample twice, resuming from the rows already written.

    One judging directory has one judge: a different spec refuses, so a report never mixes judges.
    """
    stored = judge_dir / "judge.json"
    if stored.exists() and json.loads(stored.read_text()) != spec.record():
        raise SystemExit(
            f"{judge_dir} was judged by {stored.read_text()}; one judge judges every item. "
            f"Judge with that spec, or use another --run-id"
        )
    stored.write_text(json.dumps(spec.record(), indent=2))
    judge = Judge(spec, judge_dir)
    sample = math.ceil(len(items) * rejudge_fraction)
    rejudge = random.Random(seed + 1).sample(items, sample)
    work = [(i, "verdicts.jsonl") for i in items] + [(i, "rejudge.jsonl") for i in rejudge]
    done = {
        name: {json.loads(line)["id"] for line in (judge_dir / name).read_text().splitlines()}
        if (judge_dir / name).exists()
        else set()
        for name in ("verdicts.jsonl", "rejudge.jsonl")
    }
    lock = threading.Lock()

    def one(job: tuple[dict, str]) -> None:
        item, name = job
        if item["id"] in done[name]:
            return
        row = grade(item, judge)
        with lock, (judge_dir / name).open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(item["id"], name, "ok" if row["verdict"] else "FAILED", flush=True)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(one, work))
