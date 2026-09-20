from __future__ import annotations

from pathlib import Path


def test_generated_protocol_codec_matches_runtime_canonical_bytes() -> None:
    root = Path(__file__).parents[3]
    runtime = (root / "src/exomem/hosted_activation_ack_protocol.py").read_bytes()
    generated = (
        root / "infra/provisioner/src/exomem_provisioner/hosted_activation_ack_protocol.py"
    ).read_bytes()
    header, separator, body = generated.partition(b"\n\n")

    assert separator == b"\n\n"
    assert header == b"# Generated from src/exomem/hosted_activation_ack_protocol.py; do not edit."
    assert body == runtime
