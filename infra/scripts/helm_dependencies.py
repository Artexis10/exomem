#!/usr/bin/env python3
"""Build a chart's locked dependencies with repositories from Chart.yaml."""

import argparse
import subprocess
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chart", type=Path)
    parser.add_argument("--helm", default="helm")
    args = parser.parse_args()
    chart = yaml.safe_load((args.chart / "Chart.yaml").read_text(encoding="utf-8"))
    # Chart.yaml owns this list; Helm needs registered repositories before a clean build.
    for dependency in chart.get("dependencies", []):
        subprocess.run(
            [args.helm, "repo", "add", dependency["name"], dependency["repository"]], check=True
        )
    subprocess.run([args.helm, "dependency", "build", str(args.chart)], check=True)


if __name__ == "__main__":
    main()
