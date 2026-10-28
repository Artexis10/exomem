#!/usr/bin/env python3
"""Copy or check the CRD produced by infra/topolvm/build.sh generate."""

import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("generated_root", type=Path, help="canonical provider build output")
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
source = args.generated_root / "config/crd/bases/topolvm.io_logicalvolumes.yaml"
target = Path(__file__).parent / "files/topolvm/logicalvolumes.yaml"
content = source.read_bytes()
if args.check:
    if target.read_bytes() != content:
        raise SystemExit("reservation CRD differs from the generated provider schema")
    print("Reservation CRD matches the generated provider schema.")
else:
    target.write_bytes(content)
