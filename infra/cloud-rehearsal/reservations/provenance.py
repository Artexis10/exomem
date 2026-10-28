"""Record the source inputs and compiled outputs of one disposable provider build."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

repository, assembled = map(Path, sys.argv[1:])


def output(*args):
    return subprocess.check_output(args, cwd=repository, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# These are build inputs, not a configurable product vocabulary.
inputs = [
    repository / "infra/topolvm/source.patch",
    repository / "infra/topolvm/build.sh",
    repository / "infra/topolvm/upstream-commit",
    repository / "infra/tool-versions.env",
    repository / ".github/workflows/cloud-rehearsal.yml",
]
inputs += [
    path
    for directory in ("infra/topolvm/overlay", "infra/cloud-rehearsal/reservations")
    for path in (repository / directory).rglob("*")
    if path.is_file()
]
outputs = [
    "build/lvmd",
    "build/hypertopolvm",
    "build/reservation-host-proof",
    "config/crd/bases/topolvm.io_logicalvolumes.yaml",
]
provenance = {
    "schema": "exomem-local-provider-build-v1",
    "exomem_source_sha": output("git", "rev-parse", "HEAD"),
    "exomem_source_tree": output("git", "rev-parse", "HEAD^{tree}"),
    "exomem_source_clean": not bool(
        output("git", "status", "--porcelain", "--untracked-files=all")
    ),
    "upstream_commit": (assembled / "upstream-commit").read_text().strip(),
    "go_identity": output("go", "version"),
    "inputs": {str(path.relative_to(repository)): digest(path) for path in sorted(inputs)},
    "outputs": {name: digest(assembled / name) for name in outputs},
}
(assembled / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
