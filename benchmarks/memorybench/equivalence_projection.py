"""Project a MemoryBench export into the differ's right-hand side.

`lme/runner.py` writes `equivalence.json` for the direct lane; nothing wrote
one for the guest lane, so the differ had only a left side. This mirrors that
emitter's twelve keys from the public export plus the private gold mapping.

A key the export could not source stays `None`. That is deliberate: the differ
treats null as never equal to anything, including another null, so an unsourced
key becomes a difference demanding an explanation rather than a silent pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

SCHEMA = "equivalence-input.v1"

#: The subset of readiness the direct emitter compares; `evidence` is prose.
_READINESS_FIELDS = ("lane", "requested", "verified", "method", "fallback_detected")


def _prompt_digest(question_text: str) -> str:
    return hashlib.sha256(question_text.encode("utf-8")).hexdigest()


def _project_case(
    case: dict[str, Any],
    *,
    question_id: str,
    case_set: list[str],
    container_tag: str | None,
    dataset: dict[str, Any],
    session_normalization: str | None,
    readiness: list[dict[str, Any]] | None,
    judge_config: dict[str, Any],
) -> dict[str, Any]:
    from lme.reader import CONTEXT_SEPARATOR
    from lme.exomem_capture import CAPTURE_CONTRACT

    search = case.get("search")
    ingest = case.get("ingest")
    retrieved_text = [hit["content"] for hit in case.get("hits") or []]
    question_text = (case.get("question") or {}).get("text") or ""
    canonical = session_normalization == CAPTURE_CONTRACT
    if canonical:
        readiness = case.get("readiness")

    return {
        "case_id": question_id,
        "dataset_identity": dataset,
        "case_set": case_set,
        "session_normalization": session_normalization,
        # The guest derives its namespace from the container tag, so the
        # pattern is what can be compared across runs, not the literal.
        "namespace": case.get("namespace_pattern") if canonical else (f"memorybench.container-tag/{container_tag}" if container_tag else None),
        "ingestion_payloads": (ingest or {}).get("product_payload_sha256" if canonical else "transmitted_payload_sha256"),
        "readiness": (
            [{field: lane[field] for field in _READINESS_FIELDS} for lane in readiness]
            if readiness is not None
            else None
        ),
        "exact_query": (search or {}).get("transmitted_query"),
        "top_k": ((search or {}).get("options") or {}).get("limit"),
        "retrieved_ids": (search or {}).get("normalized_hit_ids"),
        "retrieved_text": retrieved_text,
        "packed_context": CONTEXT_SEPARATOR.join(retrieved_text),
        "answer_judge_prompt_model_config": {
            **judge_config,
            "prompt_sha256": _prompt_digest(question_text),
        },
    }


def project_export(
    export: dict[str, Any],
    private_golds: dict[str, dict[str, Any]],
    *,
    judge_config: dict[str, Any],
) -> dict[str, Any]:
    """Build one `equivalence-input.v1` envelope from an export and its gold.

    `private_golds` is keyed by `case_id_hmac_sha256`; the public artifact
    carries only pseudonyms, and the comparison needs the real question ids.
    """

    from protocol.models import MemoryBenchExport

    if not isinstance(export, dict) or not {"protocol_version", "schema_version", "artifact_type", "status"} <= set(export):
        raise ValueError("projection requires an explicitly versioned MemoryBench export")
    export = MemoryBenchExport.model_validate(export).model_dump(mode="json")
    if export["status"] != "complete":
        raise ValueError("equivalence projection requires a complete MemoryBench export")
    cases = export["cases"]
    resolved: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for case in cases:
        digest = case.get("case_id_hmac_sha256")
        gold = private_golds.get(digest)
        if gold is None or not isinstance(gold.get("question_id"), str):
            raise ValueError(f"no private gold mapping for case digest {digest}")
        resolved.append((case, gold))

    case_set = sorted(gold["question_id"] for _case, gold in resolved)
    return {
        "schema": SCHEMA,
        "run_id": export.get("run_id"),
        "provider_variant": export.get("provider_variant"),
        "cases": [
            _project_case(
                case,
                question_id=gold["question_id"],
                case_set=case_set,
                container_tag=gold.get("container_tag"),
                dataset=export.get("dataset"),
                session_normalization=export.get("session_normalization"),
                readiness=export.get("readiness"),
                judge_config=judge_config,
            )
            for case, gold in resolved
        ],
    }


def _load_private_golds(directory: Path) -> dict[str, dict[str, Any]]:
    """Read the mode-0700 private-gold directory beside an export."""

    golds: dict[str, dict[str, Any]] = {}
    if not directory.is_dir():
        return golds
    for child in sorted(directory.iterdir()):
        if child.suffix != ".json" or stat.S_ISLNK(child.lstat().st_mode):
            continue
        gold = json.loads(child.read_text(encoding="utf-8"))
        digest = gold.get("case_id_hmac_sha256")
        if isinstance(digest, str):
            golds[digest] = gold
    return golds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument(
        "--private-gold", type=Path,
        help="private-gold directory; defaults to `private-gold/` beside the export",
    )
    parser.add_argument("--out", required=True, type=Path, help="run directory to write into")
    parser.add_argument("--reader", default="stub")
    parser.add_argument("--reader-model", default="gpt-4o")
    parser.add_argument("--judge-model", default="gpt-4o")
    args = parser.parse_args(argv)

    from .export import _load_json_bytes

    export = _load_json_bytes(args.export.read_bytes(), "MemoryBench export")
    gold_dir = args.private_gold or (args.export.parent / "private-gold")
    payload = project_export(
        export,
        _load_private_golds(gold_dir),
        judge_config={
            "reader": args.reader,
            "reader_model": args.reader_model,
            "judge_model": args.judge_model,
        },
    )
    args.out.mkdir(parents=True, exist_ok=True)
    destination = args.out / "equivalence.json"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
