"""Offline command line entry points for protocol maintenance and validation."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from .canary import evaluate_probes
from .leakage import scan_ingest
from .manifest import load_manifest
from .models import CaseGold, export_json_schemas
from .probes import classify_update_outcome

_ROOT = Path(__file__).resolve().parent


def _export_schemas(check: bool) -> int:
    target = _ROOT / "schema"
    if check:
        with tempfile.TemporaryDirectory() as temp:
            fresh = {path.name: path.read_bytes() for path in export_json_schemas(Path(temp))}
        committed = {path.name: path.read_bytes() for path in target.glob("*.schema.json")}
        if fresh != committed:
            print("protocol schema drift detected")
            return 1
        return 0
    for path in export_json_schemas(target):
        print(path)
    return 0


def _selftest() -> int:
    gold = CaseGold(case_id="fixture", answer="violet cedar lantern", answer_session_ids=["answer_fixture"], question_type="knowledge-update", question="Which lantern?")
    assert scan_ingest({"body": "plain source"}, gold) == ()
    assert evaluate_probes({"presence": True, "cross_case": False, "never_ingested": False}) == "isolated"
    assert classify_update_outcome(["current"]) == "superseded"
    print("protocol selftest: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark-protocol")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export-schemas")
    export.add_argument("--check", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--run-dir", required=True)
    validate.add_argument("--strict", action="store_true")
    selftest = sub.add_parser("selftest")
    selftest.add_argument("--fixtures", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "export-schemas":
        return _export_schemas(args.check)
    if args.command == "validate":
        try:
            print(load_manifest(args.run_dir).status)
        except Exception as exc:
            print(f"invalid manifest: {exc}")
            return 2 if args.strict else 1
        return 0
    return _selftest()


if __name__ == "__main__":
    raise SystemExit(main())
