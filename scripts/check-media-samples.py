"""Fail unless every media kind the image serves extracts from a real sample.

Usage: python check-media-samples.py <samples directory> <engines>

`<engines>` is the comma-separated engines this image ships, such as
`documents,ocr`. Every kind in the media registry whose engine the image ships, or
which needs no engine, must have a sample, and each sample must extract with the
phrase `expected.json` gives for it.

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

from exomem import extract, media_engines, media_types


def _squash(text: str) -> str:
    # OCR spaces CJK words apart and documents wrap lines; compare without whitespace.
    return "".join(unicodedata.normalize("NFC", text).split()).casefold()


def main(root: Path, engines: set[str]) -> int:
    expected = json.loads((root / "expected.json").read_text(encoding="utf-8"))
    failures: list[str] = []
    unknown = engines.difference(media_engines.ENGINES)
    if unknown:
        failures.append(f"unknown engines: {', '.join(sorted(unknown))}")
    served = {
        kind
        for kind in media_types.KINDS
        if media_engines.engine_for(kind) is None or media_engines.engine_for(kind) in engines
    }
    samples = sorted(path for path in root.iterdir() if path.name != "expected.json")
    covered = {media_types.media_type_for(path) for path in samples}
    for kind in sorted(served - covered):
        failures.append(f"no sample for media kind {kind!r}, which this image serves")
    for path in samples:
        if media_types.media_type_for(path) not in served:
            print(f"skip {path.name}: its engine is not shipped in this image")
            continue
        phrase = expected.get(path.name)
        if phrase is None:
            failures.append(f"{path.name}: no phrase in expected.json")
            continue
        try:
            result = extract.extract_text(path)
        except Exception as error:  # noqa: BLE001 - every failure is reported, then the build fails
            failures.append(f"{path.name}: {type(error).__name__}: {error}")
            continue
        if _squash(phrase) in _squash(result.text):
            print(f"ok {path.name} via {result.engine}")
        else:
            failures.append(f"{path.name}: phrase {phrase!r} missing from {result.text[:160]!r}")
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
    sys.exit(main(Path(sys.argv[1]), {part.strip() for part in sys.argv[2].split(",") if part.strip()}))
