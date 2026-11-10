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

from . import billing
from .isolation import claude_env

#: A judge backend per model family; the choice is the operator's, the default model is the strongest one.
DEFAULT_MODELS = {"claude": "claude-opus-5-5", "codex": "gpt-6.1-sol"}
#: The Codex judge's reasoning effort. The Claude judge takes the CLI default, as before.
DEFAULT_EFFORT = {"claude": None, "codex": "high"}
#: The judge a decision needs (PREREGISTRATION section 7, sequence 10): Claude Opus on the Claude
#: subscription. A held-out report judged by anything else never reads as decision evidence.
DECISION_JUDGE = ("claude", DEFAULT_MODELS["claude"])
#: How a judge backend relates to the actor, which always runs through the Claude CLI. A Claude judge is
#: the actor's own model family, so its reports say so; a Codex judge re-judges a sample for agreement only.
JUDGE_FAMILY = {"claude": "same as actor", "codex": "other than actor"}


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

    @property
    def family(self) -> str:
        """The judge's model family against the actor's, for the report."""
        return JUDGE_FAMILY[self.backend]


def require_key_matches_billing(env: dict[str, str]) -> None:
    """Refuse a judge whose environment would bill another account than the declared one.

    The judge's json result names no credential source, so the environment is the only evidence: the
    CLI bills the key when ``ANTHROPIC_API_KEY`` is set, and the login when it is not.
    """
    declared = billing.declared()
    if bool(env.get("ANTHROPIC_API_KEY")) != (declared == "credits"):
        raise SystemExit(
            f"the run declares {billing.ENV_VAR}={declared}, but ANTHROPIC_API_KEY is "
            f"{'set' if env.get('ANTHROPIC_API_KEY') else 'not set'} for the judge: "
            "set both to the same account"
        )


_STDIN_PROMPT = "<prompt on stdin>"


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
        if spec.backend == "claude":
            require_key_matches_billing(self.env)

    def argv(self, schema: dict, out: Path) -> list[str]:
        """The judge's command. Both backends read the prompt from stdin: a matter's deliverable can
        outgrow one argv string, and an argv is readable by every process on the machine."""
        spec = self.spec
        if spec.backend == "claude":
            executable = spec.executable or str(discover_agent_envelope().executable)
            argv = claude_argv(
                executable=executable,
                model=spec.model,
                effort=spec.effort,
                schema=schema,
                prompt=_STDIN_PROMPT,
                mcp_config=self.empty_mcp,
            )
            # ``claude -p`` with no prompt argument reads its prompt from stdin.
            argv.remove(_STDIN_PROMPT)
            return argv
        self.schema_file.write_text(json.dumps(schema, indent=2))
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
        argv = self.argv(schema, out)
        codex = self.spec.backend == "codex"
        # Codex writes its verdict to ``out``. A file left by an earlier call would otherwise read
        # as this call's verdict whenever the CLI wrote nothing.
        out.unlink(missing_ok=True)
        try:
            proc = subprocess.run(  # noqa: S603 - argv from the shared judge builders
                argv,
                input=prompt,
                cwd=self.cwd,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return Judgement(None, 0.0, [], f"timeout after {timeout} s")
        tail = f"{proc.stdout[-200:]} {proc.stderr[-200:]}"
        if proc.returncode != 0:
            return Judgement(None, 0.0, [], f"judge exited with {proc.returncode}: {tail}")
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
    pass_name: str,
    timeout: float,
    problem: Callable[[dict], str | None],
    attempts: int = 3,
) -> dict:
    """One item's verdict row: retry until the verdict has the schema's shape and ``problem`` finds nothing wrong.

    ``pass_name`` ("verdicts" or "rejudge") keeps the first judgement's output file apart from the second's.
    """
    cost, errors, reported = 0.0, [], []
    for attempt in range(attempts):
        call = judge.call(
            prompt,
            schema,
            out=out_dir / f"{item_id}.{pass_name}.{attempt}.codex-out.json",
            timeout=timeout,
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


def read_rows(path: Path) -> list[dict]:
    """The rows of a JSON-lines file; a file that does not exist yet has none."""
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def failed_judgements(judge_dir: Path) -> list[str]:
    """The judgements that still have no verdict: a retry's row follows its failed row, so only items never settled count."""
    failed = set()
    for name in ("verdicts", "rejudge"):
        rows = read_rows(judge_dir / f"{name}.jsonl")
        settled = {row["id"] for row in rows if row["verdict"]}
        failed |= {f"{row['id']} ({name})" for row in rows if row["id"] not in settled}
    return sorted(failed)


def write_sample(judge_run: Path, sample_run: Path, fraction: float, seed: int) -> int:
    """Copy a seeded random share of ``judge_run``'s items into ``sample_run`` as its whole item set.

    A second judge then judges ``sample_run`` once, in files that the first judge's report never
    reads, so it cannot change a decision. ``seed + 1`` already picks a judge's own re-judge sample, so
    this one takes ``seed + 2``. Returns how many items it holds; repeating the call over unchanged items is a no-op.
    """
    lines = (judge_run / "judge" / "items.jsonl").read_text().splitlines(keepends=True)
    chosen = random.Random(seed + 2).sample(lines, math.ceil(len(lines) * fraction))
    target = sample_run / "judge" / "items.jsonl"
    if target.exists():
        if target.read_text() != "".join(chosen):
            raise SystemExit(
                f"{target} holds another sample; move {sample_run} aside to sample again"
            )
        return len(chosen)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(chosen))
    return len(chosen)


def judge_all(
    judge_dir: Path,
    spec: JudgeSpec,
    items: list[dict],
    grade: Callable[[dict, Judge, str], dict],
    *,
    rejudge_fraction: float,
    jobs: int,
    seed: int,
) -> None:
    """Judge every item once and a sample twice, resuming from the rows already written.

    One judging directory has one judge: a different spec refuses, so a report never mixes judges.
    """
    stored = judge_dir / "judge.json"
    # The claude judge's billing is declared, not observed: its json result carries no credential source.
    # A judging directory from before the billing field ran on the subscription login.
    claude = spec.backend == "claude"
    record = {**spec.record(), **({"billing": billing.declared()} if claude else {})}
    if stored.exists():
        have = json.loads(stored.read_text())
        if claude:
            have.setdefault("billing", "subscription")
        if have != record:
            raise SystemExit(
                f"{judge_dir} was judged by {stored.read_text()}; one judge judges every item. "
                f"Judge with that spec and billing ({json.dumps(record)}), or use another --run-id"
            )
    stored.write_text(json.dumps(record, indent=2))
    judge = Judge(spec, judge_dir)
    sample = math.ceil(len(items) * rejudge_fraction)
    rejudge = random.Random(seed + 1).sample(items, sample)
    work = [(i, "verdicts") for i in items] + [(i, "rejudge") for i in rejudge]
    # Only a row with a verdict is done: a rerun retries every item whose judgement failed.
    done = {
        name: {row["id"] for row in read_rows(judge_dir / f"{name}.jsonl") if row["verdict"]}
        for name in ("verdicts", "rejudge")
    }
    lock = threading.Lock()

    def one(job: tuple[dict, str]) -> None:
        item, name = job
        if item["id"] in done[name]:
            return
        row = grade(item, judge, name)
        with lock, (judge_dir / f"{name}.jsonl").open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(item["id"], name, "ok" if row["verdict"] else "FAILED", flush=True)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(one, work))
