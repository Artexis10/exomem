"""`exomem demo` — the packaged 30-second proof (OpenSpec: add-one-command-onboarding).

Runs exomem's core loop (doctor → find → get → audit) against a sample vault
that ships INSIDE the wheel, so `uvx exomem demo` works on a machine with no
clone, no config, and no vault. This is the pre-commitment step of onboarding:
show a working `find` before asking the user to point exomem at their notes.

The bundled vault is copied to a temp directory first — site-packages may be
read-only, and no demo step may ever mutate the installed package. Lean env
flags (embeddings/media/CLIP off) are set for the duration of the run and
restored afterwards, so an in-process caller (tests) sees no env bleed.

Deterministic CLI plumbing over existing leaf functions — no reasoning surface.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

SAMPLE_VAULT = Path(__file__).resolve().parent / "_sample_vault"
TARGET_PATH = "Knowledge Base/Notes/Insights/retrieval-needs-owned-files.md"

# Correctly-spelled lean pins (the retired repo scripts carried rename-corrupted
# variants of these names, which made them silent no-ops).
LEAN_ENV = {
    "EXOMEM_DISABLE_EMBEDDINGS": "1",
    "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
    "EXOMEM_DISABLE_CLIP": "1",
    "EXOMEM_DISABLE_RELEVANCE_CHECK": "1",
    "EXOMEM_DISABLE_QUERY_LOG": "1",
    "EXOMEM_DISABLE_RANKING_CONFIG": "1",
    "EXOMEM_DISABLE_WARMUP": "1",
}


@dataclass
class StepResult:
    name: str
    ok: bool
    seconds: float
    detail: str = ""


def _materialize_vault(override: Path | None) -> tuple[Path, bool]:
    """The vault to demo against: `--vault` as-is, else a temp copy of the
    packaged sample (never the package dir itself). Returns (vault, is_temp)."""
    if override is not None:
        return override, False
    tmp = Path(tempfile.mkdtemp(prefix="exomem-demo-"))
    shutil.copytree(SAMPLE_VAULT / "Knowledge Base", tmp / "Knowledge Base")
    return tmp, True


def _excerpt(body: str) -> str:
    lines = [line.strip() for line in body.splitlines()]
    for index, line in enumerate(lines):
        if line == "## Claim":
            for candidate in lines[index + 1 :]:
                if candidate and not candidate.startswith("#"):
                    return candidate
    for line in lines:
        if line and not line.startswith("#"):
            return line
    return ""


def run_demo(
    *,
    vault: Path | None = None,
    keep: bool = False,
    json_out: bool = False,
    echo=print,
) -> int:
    """Run the four timed proof steps. Returns the process exit code."""
    saved = {k: os.environ.get(k) for k in (*LEAN_ENV, "EXOMEM_VAULT_PATH")}
    target: Path | None = None
    is_temp = False
    steps: list[StepResult] = []
    lines: list[str] = []
    t_start = time.perf_counter()
    try:
        target, is_temp = _materialize_vault(vault)
        os.environ.update(LEAN_ENV)
        os.environ["EXOMEM_VAULT_PATH"] = str(target)
        from exomem import audit, doctor, find, get_page

        def _timed(name: str, fn) -> StepResult:
            t0 = time.perf_counter()
            try:
                ok, detail = fn()
            except Exception as e:  # noqa: BLE001 — a step crash is a failed step
                ok, detail = False, f"{type(e).__name__}: {e}"
            result = StepResult(name, ok, round(time.perf_counter() - t0, 3), detail)
            steps.append(result)
            return result

        def _doctor() -> tuple[bool, str]:
            report = doctor.doctor(vault=str(target), profile="lean")
            if not report.success:
                fails = [c.id for c in report.checks if c.status == "fail"]
                return False, "failed checks: " + ", ".join(fails)
            return True, "lean profile"

        def _find() -> tuple[bool, str]:
            hits = find.find(target, query="retrieval", mode="keyword", limit=3, graph=False)
            paths = [h.path for h in hits]
            if TARGET_PATH not in paths:
                return False, "expected retrieval insight was not returned"
            lines.extend(f"   - {p}" for p in paths)
            return True, f"{len(paths)} hit(s)"

        def _get() -> tuple[bool, str]:
            page = get_page.get_page(target, path=TARGET_PATH)
            title = page.frontmatter.get("title", "Retrieval needs owned files")
            lines.append(f"   - title: {title}")
            lines.append(f"   - type: {page.frontmatter.get('type', '-')}")
            lines.append(f"   - excerpt: {_excerpt(page.body)}")
            return True, str(title)

        def _audit() -> tuple[bool, str]:
            report = audit.audit(target, categories=["broken_wikilink", "unprocessed_source"])
            if report.findings:
                probs = [f"{f.category}: {f.path}" for f in report.findings]
                return False, "; ".join(probs)
            return True, "broken_wikilink, unprocessed_source"

        if not json_out:
            echo("exomem demo — bundled sample vault, keyword mode, fully local")
            echo(f"vault: {target}")
            echo("")

        for label, fn, prefix in (
            ("doctor", _doctor, "1. doctor"),
            ("find", _find, '2. find "retrieval"'),
            ("get", _get, "3. get retrieval insight"),
            ("audit", _audit, "4. audit"),
        ):
            result = _timed(label, fn)
            if not json_out:
                status = "PASS" if result.ok else f"FAIL - {result.detail}"
                echo(f"{prefix}: {status} ({result.seconds:.2f}s)")
                for extra in lines:
                    echo(extra)
                lines.clear()
            if not result.ok:
                break

        total = round(time.perf_counter() - t_start, 3)
        success = all(s.ok for s in steps) and len(steps) == 4
        if json_out:
            envelope: dict = {
                "success": success,
                "steps": [{"name": s.name, "ok": s.ok, "seconds": s.seconds} for s in steps],
                "total_seconds": total,
            }
            if keep and is_temp:
                envelope["vault"] = str(target)
            echo(json.dumps(envelope))
        else:
            echo("")
            if success:
                echo(f"demo PASS — total {total:.2f}s. This is your proof: agents search files you own.")
                echo("Next: connect your own vault with `exomem setup`")
            else:
                echo(f"demo FAIL — total {total:.2f}s", )
            if keep and is_temp:
                echo(f"kept sample vault at: {target}")
        return 0 if success else 1
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if is_temp and not keep and target is not None:
            shutil.rmtree(target, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem demo",
        description=(
            "Prove exomem works in ~30 seconds: run doctor -> find -> get -> audit "
            "against a bundled sample vault. No clone, no config, no vault needed; "
            "fully local, read-only, keyword mode."
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit a stable JSON envelope (for CI)")
    parser.add_argument(
        "--keep", action="store_true",
        help="keep the temp sample-vault copy and print its path (open it in Obsidian)",
    )
    parser.add_argument(
        "--vault", default=None,
        help="re-run the steps against a sample copy kept earlier via --keep "
        "(the checks are sample-specific — this is not for your own vault)",
    )
    args = parser.parse_args(argv)
    # Human output uses em-dashes; a redirected legacy-codepage Windows stdout
    # must degrade to replacement chars, never a UnicodeEncodeError mid-demo.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass
    try:
        return run_demo(
            vault=Path(args.vault).expanduser() if args.vault else None,
            keep=args.keep,
            json_out=args.json,
        )
    except Exception as e:  # noqa: BLE001 — top-level CLI guard, mirror _serve_main
        print(f"exomem demo failed: {e}", file=sys.stderr)
        return 1
