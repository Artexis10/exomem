#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
generator="$root/tests/fixtures/documents_cpu/generate.py"
run_dir=$(mktemp -d /tmp/exomem-documents-cpu.XXXXXX)
tag="exomem-documents-cpu:${run_dir##*/}"
export BUILDX_CONFIG="${BUILDX_CONFIG:-$run_dir/buildx}"
fixtures="$run_dir/inputs"
before="$run_dir/input-hashes-before"
after="$run_dir/input-hashes-after"
help_output="$run_dir/help-output"

cleanup() {
    docker image rm "$tag" >/dev/null 2>&1 || true
    rm -rf "$run_dir"
}
trap cleanup EXIT

command -v docker >/dev/null
test -f "$generator"
mkdir -p "$BUILDX_CONFIG"
mkdir -p "$fixtures"
printf 'acceptance_tag=%s run_dir=%s\n' "$tag" "$run_dir"

docker build --target documents-cpu --tag "$tag" "$root"

docker run --rm --network=none --read-only --cap-drop=ALL --memory=512m --cpus=1.0 \
    --security-opt no-new-privileges=true \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$generator,dst=/generator/generate.py,readonly" \
    --mount "type=bind,src=$fixtures,dst=/generated" \
    --entrypoint /app/.venv/bin/python \
    "$tag" /generator/generate.py /generated

sha256sum "$fixtures"/* | sort >"$before"

image_bytes=$(docker image inspect "$tag" --format '{{.Size}}')
image_id=$(docker image inspect "$tag" --format '{{.Id}}')
started_ns=$(date +%s%N)
docker run --rm --network=none --read-only --cap-drop=ALL --memory=512m --cpus=1.0 \
    --security-opt no-new-privileges=true \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    "$tag" --help >"$help_output"
finished_ns=$(date +%s%N)
startup_ms=$(((finished_ns - started_ns) / 1000000))

docker run --rm --network=none --read-only --cap-drop=ALL --memory=512m --cpus=1.0 \
    --security-opt no-new-privileges=true \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    --mount "type=bind,src=$fixtures,dst=/inputs,readonly" \
    --entrypoint /app/.venv/bin/python \
    -i "$tag" - <<'PY'
from __future__ import annotations

import hashlib
import importlib.metadata
import socket
from pathlib import Path


egress_attempts = []


def deny_egress(name):
    def denied(*args, **kwargs):
        egress_attempts.append((name, repr(args), repr(kwargs)))
        raise AssertionError(f"document extraction attempted network egress through {name}")

    return denied


socket.create_connection = deny_egress("create_connection")
socket.getaddrinfo = deny_egress("getaddrinfo")
socket.gethostbyname = deny_egress("gethostbyname")
socket.socket.connect = deny_egress("connect")
socket.socket.connect_ex = deny_egress("connect_ex")

installed = {
    distribution.metadata["Name"].lower()
    for distribution in importlib.metadata.distributions()
    if distribution.metadata.get("Name")
}
for distribution in ("torch", "faster-whisper", "ctranslate2"):
    assert distribution not in installed, f"unexpected CPU document image dependency: {distribution}"
assert not {distribution for distribution in installed if distribution.startswith("nvidia-")}

from exomem import extract

root = Path("/inputs")
hashes_before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.iterdir()}

expected = {
    "multi-sheet.xlsx": ("Summary", "Oranges", "Archive", "North"),
    "table.docx": ("DOCX TABLE HEADING", "Item", "Widget", "Quantity"),
    "multi-slide.pptx": ("Slide One Heading", "First slide detail", "Slide Two Heading"),
    "mixed-searchable-scanned.pdf": ("PDF SEARCHABLE ALPHA", "SCANNED PDF BRAVO 73"),
    "image-ocr.png": ("IMAGE OCR 42",),
}

for name, snippets in expected.items():
    result = extract.extract_text(root / name)
    for snippet in snippets:
        assert snippet in result.text, (name, snippet, result.text)

pdf_result = extract.extract_text(root / "mixed-searchable-scanned.pdf")
assert pdf_result.engine == "pymupdf+tesseract"
assert any("scanned page(s) recovered via OCR" in warning for warning in pdf_result.warnings)

for name, message in {
    "corrupt.docx": "markitdown could not convert",
    "protected.pdf": "password-protected",
    "unsupported.bin": "no extractor",
    "macro.docm": "no extractor",
}.items():
    try:
        extract.extract_text(root / name)
    except extract.ExtractionUnavailable as error:
        assert message in str(error), (name, error)
    else:
        raise AssertionError(f"{name} unexpectedly extracted")

hashes_after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.iterdir()}
assert hashes_after == hashes_before
assert not egress_attempts, egress_attempts
print(f"cgroup_memory_peak_bytes={Path('/sys/fs/cgroup/memory.peak').read_text().strip()}")
print("offline document/OCR extraction accepted")
PY

sha256sum "$fixtures"/* | sort >"$after"
cmp "$before" "$after"
printf 'local_measurement image_id=%s image_bytes=%s startup_ms=%s\n' "$image_id" "$image_bytes" "$startup_ms"
cat "$help_output" >/dev/null
