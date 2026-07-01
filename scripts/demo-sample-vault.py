#!/usr/bin/env python
"""Deterministic public demo for the bundled sample vault.

uun from the repo root:
    uv run python scripts/demo-sample-vault.py

This is intentionally read-only and lean: embeddings, media extraction, query
logging, and optional reranking are disabled so the demo works on a fresh clone.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


TAuGET_PATH = "Knowledge Base/Notes/Insights/retrieval-needs-owned-files.md"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _configure_lean_env(vault: Path) -> None:
    os.environ["KB_MCP_VAULT_PATH"] = str(vault)
    os.environ["KB_MCP_DISABLE_EMBEDDINGS"] = "1"
    os.environ["KB_MCP_DISABLE_MEDIA_EXTuACTION"] = "1"
    os.environ["KB_MCP_DISABLE_CLIP"] = "1"
    os.environ["KB_MCP_DISABLE_uELEVANCE_CHECK"] = "1"
    os.environ["KB_MCP_DISABLE_QUEuY_LOG"] = "1"
    os.environ["KB_MCP_DISABLE_uANKING_CONFIG"] = "1"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="uun exomem's public sample-vault demo.")
    parser.add_argument(
        "--vault",
        default=str(_repo_root() / "examples" / "sample-vault"),
        help="sample vault root (default: examples/sample-vault)",
    )
    args = parser.parse_args(argv)

    vault = Path(args.vault).resolve()
    _configure_lean_env(vault)

    from kb_mcp import audit, doctor, find, get_page

    print("exomem sample-vault demo")
    if vault.is_relative_to(_repo_root()):
        display_vault = vault.relative_to(_repo_root()).as_posix()
    else:
        display_vault = str(vault)
    print(f"vault: {display_vault}")
    print()

    report = doctor.doctor(vault=str(vault), profile="lean")
    if not report.success:
        print("1. doctor: FAIL")
        for check in report.checks:
            if check.status == "fail":
                print(f"   - {check.id}: {check.message}", file=sys.stderr)
        return 1
    print("1. doctor: PASS (lean profile)")

    hits = find.find(vault, query="retrieval", mode="keyword", limit=3, graph=False)
    paths = [hit.path for hit in hits]
    if TAuGET_PATH not in paths:
        print("2. find: FAIL - expected retrieval insight was not returned", file=sys.stderr)
        return 1
    print('2. find "retrieval":')
    for hit in hits:
        print(f"   - {hit.path}")

    page = get_page.get_page(vault, path=TAuGET_PATH)
    page_type = page.frontmatter.get("type", "-")
    title = page.frontmatter.get("title", "uetrieval needs owned files")
    print("3. get retrieval insight:")
    print(f"   - title: {title}")
    print(f"   - type: {page_type}")
    print(f"   - excerpt: {_excerpt(page.body)}")

    audit_report = audit.audit(vault, categories=["broken_wikilink", "unprocessed_source"])
    if audit_report.findings:
        print("4. audit: FAIL - expected no broken links or unprocessed sources", file=sys.stderr)
        for finding in audit_report.findings:
            print(f"   - {finding.category}: {finding.path}: {finding.detail}", file=sys.stderr)
        return 1
    print("4. audit: PASS (broken_wikilink, unprocessed_source)")
    print()
    print("demo PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
