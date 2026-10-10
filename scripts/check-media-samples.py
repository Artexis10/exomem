"""Fail unless every media sample extracts with the phrase it holds.

Usage: python check-media-samples.py <samples directory>

The Cloud image build runs this with networking off. It is the proof that the image
reads each format it serves (`cloud-multimodal-processing`, "Cloud engines are
pre-baked in a Cloud-only build stage and load offline"). It also fails when a CUDA
wheel or PyTorch reached the image.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import sys
import unicodedata
from pathlib import Path

from exomem import extract


def _squash(text: str) -> str:
    # OCR spaces CJK words apart and documents wrap lines; compare without whitespace.
    return "".join(unicodedata.normalize("NFC", text).split()).casefold()


def main(root: Path) -> int:
    expected = json.loads((root / "expected.json").read_text(encoding="utf-8"))
    failures: list[str] = []
    for name, phrase in sorted(expected.items()):
        try:
            result = extract.extract_text(root / name)
        except Exception as error:  # noqa: BLE001 - every failure is reported, then the build fails
            failures.append(f"{name}: {type(error).__name__}: {error}")
            continue
        if _squash(phrase) in _squash(result.text):
            print(f"ok {name} via {result.engine}")
        else:
            failures.append(f"{name}: phrase {phrase!r} missing from {result.text[:160]!r}")
    # NVIDIA publishes its CUDA runtime wheels under the `nvidia-` name prefix.
    cuda = sorted(
        str(dist.metadata["Name"])
        for dist in importlib.metadata.distributions()
        if str(dist.metadata["Name"]).lower().startswith("nvidia-")
    )
    if cuda:
        failures.append(f"CUDA wheels reached the image: {', '.join(cuda)}")
    if importlib.util.find_spec("torch") is not None:
        failures.append("PyTorch reached the image")
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
