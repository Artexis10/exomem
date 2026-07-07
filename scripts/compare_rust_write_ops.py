from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CRATE_ROOT = REPO_ROOT / "experiments" / "rust_find_keyword_lite"
SCRATCH_ROOT = CRATE_ROOT / "target" / "write-bench"
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures"
TODAY = "2026-07-07"


@dataclass
class SampleSet:
    operation: str
    variant: str
    samples_ms: list[float]
    note: str = ""

    def summary(self) -> dict:
        ordered = sorted(self.samples_ms)
        p90_idx = min(len(ordered) - 1, int(len(ordered) * 0.9))
        return {
            "operation": self.operation,
            "variant": self.variant,
            "n": len(ordered),
            "median_ms": round(statistics.median(ordered), 3),
            "p90_ms": round(ordered[p90_idx], 3),
            "min_ms": round(ordered[0], 3),
            "max_ms": round(ordered[-1], 3),
            "note": self.note,
        }


def main() -> int:
    args = parse_args()
    configure_env()
    sys.path.insert(0, str(REPO_ROOT / "src"))

    rust_bin = build_rust_write_bin()
    reset_dir(SCRATCH_ROOT)
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)

    results: list[SampleSet] = []
    if not args.skip_edit:
        results.extend(run_edit_bench(args, rust_bin))
    if not args.skip_note:
        results.extend(run_note_bench(args, rust_bin))

    summaries = [result.summary() for result in results]
    print(json.dumps({"summaries": summaries}, indent=2))
    print()
    print_table(summaries)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare Exomem production writes against minimal Python and Rust "
            "file-write slices. Writes only under experiments/.../target/write-bench."
        )
    )
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--pages", type=int, default=2000)
    parser.add_argument("--body-kb", type=int, default=4)
    parser.add_argument(
        "--source-vault",
        type=Path,
        default=None,
        help="Optional real vault to copy into scratch before benchmarking.",
    )
    parser.add_argument("--skip-edit", action="store_true")
    parser.add_argument("--skip-note", action="store_true")
    parser.add_argument("--keep-vaults", action="store_true")
    parsed = parser.parse_args()
    parsed.samples = max(3, parsed.samples)
    parsed.pages = max(1, parsed.pages)
    parsed.body_kb = max(1, parsed.body_kb)
    return parsed


def configure_env() -> None:
    os.environ.setdefault("EXOMEM_DISABLE_EMBEDDINGS", "1")
    os.environ.setdefault("EXOMEM_DISABLE_CLIP", "1")
    os.environ.setdefault("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    os.environ.setdefault("EXOMEM_DISABLE_QUERY_LOG", "1")
    os.environ.setdefault("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    os.environ.setdefault("EXOMEM_DISABLE_WARMUP", "1")
    os.environ.setdefault("EXOMEM_DISABLE_FILE_WATCHER", "1")
    os.environ.setdefault("EXOMEM_DISABLE_MODE_WATCH", "1")
    os.environ.setdefault("EXOMEM_DISABLE_RANKING_CONFIG", "1")


def build_rust_write_bin() -> Path:
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("cargo is not installed; cannot build Rust comparator")
    subprocess.run(
        [cargo, "build", "--release", "--manifest-path", str(CRATE_ROOT / "Cargo.toml")],
        cwd=REPO_ROOT,
        check=True,
        timeout=120,
    )
    exe = "rust_write_lite.exe" if os.name == "nt" else "rust_write_lite"
    return CRATE_ROOT / "target" / "release" / exe


def run_edit_bench(args: argparse.Namespace, rust_bin: Path) -> list[SampleSet]:
    vault_py_tool = prepare_vault("edit-python-tool", args)
    vault_py_lite = prepare_vault("edit-python-lite", args)
    vault_rust = prepare_vault("edit-rust-lite", args)
    target_rel = ensure_edit_target(vault_py_tool)
    ensure_edit_target(vault_py_lite)
    ensure_edit_target(vault_rust)
    prime_production_indexes(vault_py_tool)

    from exomem import commands

    # Warm imports, parse caches, sidecar upsert path, and log/index write shape.
    commands.op_edit(
        vault_py_tool,
        path=target_rel,
        why="write benchmark warmup",
        old_string="BENCH_MARKER_0",
        new_string="BENCH_MARKER_1",
    )

    production: list[float] = []
    for i in range(1, args.samples + 1):
        old = f"BENCH_MARKER_{i}"
        new = f"BENCH_MARKER_{i + 1}"
        t0 = time.perf_counter()
        commands.op_edit(
            vault_py_tool,
            path=target_rel,
            why="write benchmark",
            old_string=old,
            new_string=new,
        )
        production.append((time.perf_counter() - t0) * 1000.0)

    python_lite: list[float] = []
    for i in range(args.samples):
        old = f"BENCH_MARKER_{i}"
        new = f"BENCH_MARKER_{i + 1}"
        python_lite.append(python_edit_lite(vault_py_lite, target_rel, old, new))

    rust_internal: list[float] = []
    rust_process: list[float] = []
    for i in range(args.samples):
        old = f"BENCH_MARKER_{i}"
        new = f"BENCH_MARKER_{i + 1}"
        inner, wall = rust_edit_lite(rust_bin, vault_rust, target_rel, old, new)
        rust_internal.append(inner)
        rust_process.append(wall)

    if not args.keep_vaults:
        cleanup_vaults(vault_py_tool, vault_py_lite, vault_rust)

    return [
        SampleSet(
            "edit",
            "python_tool",
            production,
            "full Exomem edit: validation + log/index + lexical sidecar",
        ),
        SampleSet("edit", "python_file_lite", python_lite, "same file mutation only"),
        SampleSet("edit", "rust_file_lite", rust_internal, "same file mutation only"),
        SampleSet("edit", "rust_process_wall", rust_process, "includes process spawn"),
    ]


def run_note_bench(args: argparse.Namespace, rust_bin: Path) -> list[SampleSet]:
    vault_py_tool = prepare_vault("note-python-tool", args)
    vault_py_lite = prepare_vault("note-python-lite", args)
    vault_rust = prepare_vault("note-rust-lite", args)
    prime_production_indexes(vault_py_tool)

    from exomem import commands

    # Warm imports and write shape with one disposable production note.
    commands.op_note(
        vault_py_tool,
        content="# Bench Production Note Warmup\n\n## Claim\n\nWarmup.",
        note_type="insight",
        title="Bench Production Note Warmup",
        suggestions=False,
    )

    production: list[float] = []
    for i in range(args.samples):
        title = f"Bench Production Note {i:04d}"
        content = f"# {title}\n\n## Claim\n\nProduction write benchmark {i}."
        t0 = time.perf_counter()
        commands.op_note(
            vault_py_tool,
            content=content,
            note_type="insight",
            title=title,
            suggestions=False,
        )
        production.append((time.perf_counter() - t0) * 1000.0)

    python_lite: list[float] = []
    for i in range(args.samples):
        title = f"Bench Python Lite Note {i:04d}"
        rel = f"Knowledge Base/Notes/Insights/bench-python-lite-note-{i:04d}.md"
        content = f"# {title}\n\n## Claim\n\nLite write benchmark {i}."
        python_lite.append(python_note_lite(vault_py_lite, rel, title, content))

    rust_internal: list[float] = []
    rust_process: list[float] = []
    for i in range(args.samples):
        title = f"Bench Rust Lite Note {i:04d}"
        rel = f"Knowledge Base/Notes/Insights/bench-rust-lite-note-{i:04d}.md"
        content = f"# {title}\n\n## Claim\n\nLite write benchmark {i}."
        inner, wall = rust_note_lite(rust_bin, vault_rust, rel, title, content)
        rust_internal.append(inner)
        rust_process.append(wall)

    if not args.keep_vaults:
        cleanup_vaults(vault_py_tool, vault_py_lite, vault_rust)

    return [
        SampleSet(
            "note",
            "python_tool",
            production,
            "full Exomem note: validation + log/index + lexical sidecar",
        ),
        SampleSet("note", "python_file_lite", python_lite, "frontmatter page create only"),
        SampleSet("note", "rust_file_lite", rust_internal, "frontmatter page create only"),
        SampleSet("note", "rust_process_wall", rust_process, "includes process spawn"),
    ]


def prepare_vault(name: str, args: argparse.Namespace) -> Path:
    dest = SCRATCH_ROOT / name
    reset_dir(dest)
    if args.source_vault is not None:
        copy_source_vault(args.source_vault, dest)
    else:
        shutil.copytree(FIXTURE_VAULT, dest)
        generate_synthetic_pages(dest, args.pages, args.body_kb)
    ensure_minimal_kb_files(dest)
    ensure_project_keys(dest)
    return dest


def copy_source_vault(source: Path, dest: Path) -> None:
    source = source.resolve()
    if not (source / "Knowledge Base").is_dir():
        raise RuntimeError(f"{source} does not look like a vault (missing Knowledge Base/)")
    skip_dirs = {
        ".git",
        ".obsidian",
        ".trash",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "_attachments",
    }
    keep_suffixes = {".md", ".yaml", ".yml"}
    dest.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        if any(part in skip_dirs for part in rel.parts):
            continue
        if path.suffix.lower() not in keep_suffixes:
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def ensure_minimal_kb_files(vault: Path) -> None:
    kb = vault / "Knowledge Base"
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "index.md").write_text(
        (kb / "index.md").read_text(encoding="utf-8")
        if (kb / "index.md").exists()
        else "# Knowledge Base\n\n## Recent activity\n\n## Counts\n\n",
        encoding="utf-8",
    )
    (kb / "log.md").write_text(
        (kb / "log.md").read_text(encoding="utf-8")
        if (kb / "log.md").exists()
        else "# Log\n\n---\n",
        encoding="utf-8",
    )


def ensure_project_keys(vault: Path) -> None:
    schema = vault / "Knowledge Base" / "_Schema"
    schema.mkdir(parents=True, exist_ok=True)
    path = schema / "project-keys.yaml"
    if path.exists():
        return
    path.write_text(
        (
            "projects:\n"
            "  exomem:\n"
            "    folder: Exomem\n"
            "    category: research\n"
            "  personal:\n"
            "    folder: Personal\n"
            "    category: personal\n"
        ),
        encoding="utf-8",
    )


def generate_synthetic_pages(vault: Path, pages: int, body_kb: int) -> None:
    body_line = (
        "Synthetic write benchmark content about retrieval latency indexing "
        "frontmatter resolver keyword bm25 note edit scaling.\n"
    )
    repeat = max(1, (body_kb * 1024) // len(body_line))
    body = body_line * repeat
    root = vault / "Knowledge Base" / "Notes" / "Research" / "BenchWrite"
    root.mkdir(parents=True, exist_ok=True)
    for i in range(pages):
        marker = "BENCH_MARKER_0" if i == 0 else f"STATIC_MARKER_{i}"
        title = f"Bench Write Page {i:05d}"
        (root / f"bench-write-page-{i:05d}.md").write_text(
            (
                "---\n"
                "type: research-note\n"
                "project: exomem\n"
                "status: active\n"
                f"title: {title}\n"
                "created: 2026-01-01\n"
                "updated: 2026-01-01\n"
                "tags: [benchmark]\n"
                "---\n"
                f"# {title}\n\n{marker}\n\n{body}"
            ),
            encoding="utf-8",
        )


def ensure_edit_target(vault: Path) -> str:
    rel = "Knowledge Base/Notes/Research/BenchWrite/bench-write-page-00000.md"
    target = vault / rel
    if target.exists():
        text = target.read_text(encoding="utf-8")
        if "BENCH_MARKER_0" not in text:
            target.write_text(text + "\nBENCH_MARKER_0\n", encoding="utf-8")
        return rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        (
            "---\n"
            "type: research-note\n"
            "project: exomem\n"
            "status: active\n"
            "title: Bench Write Page 00000\n"
            "created: 2026-01-01\n"
            "updated: 2026-01-01\n"
            "tags: [benchmark]\n"
            "---\n"
            "# Bench Write Page 00000\n\nBENCH_MARKER_0\n\nSynthetic target.\n"
        ),
        encoding="utf-8",
    )
    return rel


def prime_production_indexes(vault: Path) -> None:
    from exomem import embeddings, find, lexstore

    find.clear_cache()
    embeddings.clear_embedding_indexes()
    lexstore.ensure_fresh(vault)


def python_edit_lite(vault: Path, rel: str, old: str, new: str) -> float:
    target = vault / rel
    t0 = time.perf_counter()
    text = normalize_newlines(target.read_text(encoding="utf-8"))
    frontmatter, body = split_frontmatter(text)
    count = body.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one match for {old!r}, got {count}")
    frontmatter = set_or_append_frontmatter(frontmatter, "updated", TODAY)
    body = body.replace(old, new, 1).rstrip() + "\n"
    atomic_write_text(target, f"---\n{frontmatter}\n---\n{body}")
    return (time.perf_counter() - t0) * 1000.0


def python_note_lite(vault: Path, rel: str, title: str, content: str) -> float:
    target = vault / rel
    t0 = time.perf_counter()
    note = (
        "---\n"
        "type: insight\n"
        f"title: {title}\n"
        "status: active\n"
        f"created: {TODAY}\n"
        f"updated: {TODAY}\n"
        "---\n"
        f"{normalize_newlines(content).rstrip()}\n"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, note)
    return (time.perf_counter() - t0) * 1000.0


def rust_edit_lite(
    rust_bin: Path, vault: Path, rel: str, old: str, new: str
) -> tuple[float, float]:
    t0 = time.perf_counter()
    out = subprocess.run(
        [
            str(rust_bin),
            "edit",
            "--vault",
            str(vault),
            "--path",
            rel,
            "--old",
            old,
            "--new",
            new,
            "--date",
            TODAY,
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    wall = (time.perf_counter() - t0) * 1000.0
    return float(json.loads(out.stdout)["duration_ms"]), wall


def rust_note_lite(
    rust_bin: Path, vault: Path, rel: str, title: str, content: str
) -> tuple[float, float]:
    t0 = time.perf_counter()
    out = subprocess.run(
        [
            str(rust_bin),
            "note",
            "--vault",
            str(vault),
            "--path",
            rel,
            "--title",
            title,
            "--content",
            content,
            "--date",
            TODAY,
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    wall = (time.perf_counter() - t0) * 1000.0
    return float(json.loads(out.stdout)["duration_ms"]), wall


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise RuntimeError("missing frontmatter")
    rest = text[4:]
    idx = rest.find("\n---\n")
    if idx == -1:
        raise RuntimeError("missing closing frontmatter delimiter")
    return rest[:idx], rest[idx + len("\n---\n") :]


def set_or_append_frontmatter(frontmatter: str, key: str, value: str) -> str:
    prefix = f"{key}:"
    lines = []
    replaced = False
    for line in frontmatter.splitlines():
        if line.lstrip().startswith(prefix):
            lines.append(f"{key}: {value}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.perf_counter_ns()}.tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def cleanup_vaults(*vaults: Path) -> None:
    for vault in vaults:
        reset_dir(vault)


def print_table(rows: list[dict]) -> None:
    headers = ["operation", "variant", "n", "median_ms", "p90_ms", "min_ms", "max_ms"]
    widths = {
        header: max(len(header), *(len(str(row[header])) for row in rows))
        for header in headers
    }
    print(" | ".join(header.ljust(widths[header]) for header in headers))
    print(" | ".join("-" * widths[header] for header in headers))
    for row in rows:
        print(" | ".join(str(row[header]).ljust(widths[header]) for header in headers))


if __name__ == "__main__":
    raise SystemExit(main())
