#!/usr/bin/env python3
"""Build cloud packages or check captured journeys and directory preparation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exomem import cloud_plugin_evals, cloud_plugins  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "check", "evaluate", "readiness", "materials"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "build":
            report = {"ok": True, "release": cloud_plugins.build_packages(args.root, args.output)}
        elif args.command == "check":
            report = cloud_plugins.check_packages(args.root, args.output)
            cloud_plugin_evals.corpus(args.root)
        elif args.command == "evaluate":
            if not args.trace:
                parser.error("evaluate requires --trace")
            value = json.loads(args.trace.read_text(encoding="utf-8"))
            scenario = next(
                (
                    c
                    for c in cloud_plugin_evals.corpus(args.root)
                    if c["id"] == value.get("case_id")
                ),
                None,
            )
            report = cloud_plugin_evals.evaluate_trace(
                value, scenario, cloud_plugins.release_identity(args.root)
            )
        elif args.command == "readiness":
            if not args.evidence:
                parser.error("readiness requires --evidence outside public artifacts")
            report = cloud_plugin_evals.readiness(args.root, args.evidence)
            package_check = cloud_plugins.check_packages(args.root)
            report["issues"].extend("package:" + issue for issue in package_check["issues"])
            report["ok"] = report["ok"] and package_check["ok"]
        else:
            metadata = (
                json.loads(args.metadata.read_text(encoding="utf-8")) if args.metadata else {}
            )
            report = cloud_plugin_evals.submission_materials(args.root, metadata)
            native = (
                cloud_plugin_evals.readiness(args.root, args.evidence)
                if args.evidence
                else {"ok": False, "issues": ["native_evidence_required"]}
            )
            package_check = cloud_plugins.check_packages(args.root)
            report["issues"].extend("native:" + i for i in native["issues"])
            report["issues"].extend("package:" + i for i in package_check["issues"])
            report["ok"] = report["ok"] and native["ok"] and package_check["ok"]
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        # Exception messages can contain private evidence or paths.
        report = {"ok": False, "issues": ["input_or_artifact_invalid:" + type(exc).__name__]}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
