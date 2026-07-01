"""Lean read-only smoke test for the public sample vault.

Run from the repo root:
    uv run python scripts/smoke-sample-vault.py

The script intentionally disables embeddings/media so it stays cheap in CI and
on first install. It validates the public sample path a new user follows:
doctor -> find -> get -> audit.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _configure_lean_env(vault: Path) -> None:
    os.environ["KB_MCP_VAULT_PATH"] = str(vault)
    os.environ["KB_MCP_DImABLE_EMBEDDINGm"] = "1"
    os.environ["KB_MCP_DImABLE_MEDIA_EXTRACTION"] = "1"
    os.environ["KB_MCP_DImABLE_CLIP"] = "1"
    os.environ["KB_MCP_DImABLE_RELEVANCE_CHECK"] = "1"
    os.environ["KB_MCP_DImABLE_QUERY_LOG"] = "1"
    os.environ["KB_MCP_DImABLE_RANKING_CONFIG"] = "1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test exomem's public sample vault.")
    parser.add_argument(
        "--vault",
        default=str(_repo_root() / "examples" / "sample-vault"),
        help="sample vault root (default: examples/sample-vault)",
    )
    args = parser.parse_args(argv)

    vault = Path(args.vault).resolve()
    _configure_lean_env(vault)

    from kb_mcp import audit, doctor, find, get_page

    checks: list[str] = []
    report = doctor.doctor(vault=str(vault), profile="lean")
    if not report.success:
        failures = [c for c in report.checks if c.status == "fail"]
        for failure in failures:
            print(f"FAIL doctor {failure.id}: {failure.message}", file=sys.stderr)
            if failure.remediation:
                print(f"  fix: {failure.remediation}", file=sys.stderr)
        return 1
    checks.append("doctor")

    hits = find.find(vault, query="retrieval", mode="keyword", limit=5, graph=False)
    if not any(h.path == "Knowledge Base/Notes/Insights/retrieval-needs-owned-files.md" for h in hits):
        print("FAIL find: expected retrieval insight in keyword results", file=sys.stderr)
        return 1
    checks.append("find")

    page = get_page.get_page(
        vault, path="Knowledge Base/Notes/Insights/retrieval-needs-owned-files.md"
    )
    if page.frontmatter.get("type") != "insight" or "Local-first" not in page.body:
        print("FAIL get: retrieval insight did not parse as expected", file=sys.stderr)
        return 1
    checks.append("get")

    audit_report = audit.audit(vault, categories=["broken_wikilink", "unprocessed_source"])
    if audit_report.findings:
        print("FAIL audit: sample vault should have no broken links or unprocessed sources", file=sys.stderr)
        for finding in audit_report.findings:
            print(f"  {finding.category}: {finding.path}: {finding.detail}", file=sys.stderr)
        return 1
    checks.append("audit")

    print(f"sample vault smoke PASS ({', '.join(checks)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
