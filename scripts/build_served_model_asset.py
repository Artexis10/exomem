"""Build a served model's int8 artefact and pack it as its release asset.

A host downloads the published artefact (`EXOMEM_MODEL_ARTIFACT_URL`, a GitHub
release asset by default) before it ever builds one, because the local build
peaks at about 8.7 GB of memory for bge-m3. This script produces that asset
from the same deterministic build a host would run:

    uv run --extra embeddings python scripts/build_served_model_asset.py --out dist/served-models

It builds locally (never downloading), in a fresh artefact directory under
`--out` that it removes afterwards, so it never repackages an artefact a host
fetched or built earlier. It checks the result against the digest pinned in
`embedding_backend._SERVED`, and writes `<model>-int8-<digest8>.onnx.tar`.
Upload that file to the `served-models` release; hosts verify every byte
against the pinned digest before using it. A build whose digest differs from
the pin exits non-zero: publish only an asset the pin names.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="BAAI/bge-m3", help="a served model (default: %(default)s)")
    parser.add_argument("--out", type=Path, required=True, help="directory the asset is written to")
    args = parser.parse_args(argv)

    from exomem import embedding_backend

    served = embedding_backend.served_artifact(args.model)
    if served is None:
        print(f"{args.model} is not a served model", file=sys.stderr)
        return 2
    # The asset must come from a build of its own: never from the asset it
    # replaces, and never from an artefact already on this host, which may have
    # been fetched rather than built.
    args.out.mkdir(parents=True, exist_ok=True)
    fresh = Path(tempfile.mkdtemp(prefix=".build-", dir=args.out))
    os.environ[embedding_backend.ARTIFACT_URL_ENV] = "off"
    os.environ[embedding_backend.ARTIFACT_DIR_ENV] = str(fresh)
    try:
        embedding_backend.ensure_artifact(args.model, served)
        built = embedding_backend.artifact_sha256(embedding_backend.artifact_dir(args.model, served))
        if served.digest and built != served.digest:
            print(
                f"built artefact {built} does not match the pinned digest {served.digest}; "
                "update the pin in embedding_backend._SERVED before publishing",
                file=sys.stderr,
            )
            return 1
        asset = embedding_backend.write_artifact_asset(args.model, served, args.out)
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
    tar_sha = hashlib.sha256(asset.read_bytes()).hexdigest()
    print(f"asset: {asset}")
    print(f"artefact sha256: {built}")
    print(f"asset size: {asset.stat().st_size} bytes, sha256 {tar_sha}")
    print(f"upload to: {embedding_backend.DEFAULT_ARTIFACT_URL}/{asset.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
