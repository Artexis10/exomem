"""Measure the mutation-boundary hold caused by one write against a missing graph sidecar.

Reproduces on main's code path (no fix applied). Reports, per vault size:
  - how long a SINGLE `upsert_after_write` takes when the sidecar is absent
  - how long the same write takes once the sidecar exists (the healthy case)

The gap between the two is the stall a user eats on the first write after the
sidecar is missing, deleted, schema-bumped, or registry-invalidated.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve()
REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if REPO is None:
    raise SystemExit("usage: repro_graph_hold.py <repo-root> [sizes...]")
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

for name in (
    "EXOMEM_DISABLE_EMBEDDINGS",
    "EXOMEM_DISABLE_CLIP",
    "EXOMEM_DISABLE_MEDIA_EXTRACTION",
    "EXOMEM_DISABLE_RANKING",
):
    os.environ[name] = "1"

from synth_vault import gen_dense_vault  # noqa: E402

from exomem import epistemic_graph  # noqa: E402
from exomem.kbdir import kb_dirname  # noqa: E402

SIZES = [int(a) for a in sys.argv[2:]] or [500, 2000]


def one_write(vault: Path, target: Path) -> float:
    started = time.perf_counter()
    epistemic_graph.upsert_after_write(vault, [target])
    return (time.perf_counter() - started) * 1000.0


for size in SIZES:
    with tempfile.TemporaryDirectory(prefix=f"graph-hold-{size}-") as temp:
        vault = Path(temp) / "vault"
        vault.mkdir(parents=True)
        gen_dense_vault(vault, size, links_per_note=3)

        target = next((vault / kb_dirname()).rglob("*.md"))
        sidecar = epistemic_graph.sidecar_path(vault)
        assert not sidecar.exists(), "precondition: no sidecar yet"

        cold_ms = one_write(vault, target)
        built = sidecar.exists()
        warm_ms = one_write(vault, target)

        print(
            f"pages={size:<6} first_write_missing_sidecar={cold_ms:9.1f}ms  "
            f"subsequent_write={warm_ms:7.1f}ms  "
            f"ratio={cold_ms / max(warm_ms, 0.001):7.1f}x  sidecar_built={built}",
            flush=True,
        )
