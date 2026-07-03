"""Pure corpus-counting + benchmark-report rendering for the retrieval eval.

No torch, no live model/network access, and — deliberately — no per-query or
per-path inputs. This mirrors `eval_metrics.py`'s "pure, torch-free, unit-
testable" precedent so the reporting logic is exercised on fixture data without
a real vault or downloaded models, and runs in the lean (embedding-free) test
suite.

Two responsibilities live here:

- `count_corpus_stats(vault_root)` — a plain filesystem walk that returns rounded
  `{"files", "notes", "media"}` counts. Every count is rounded DOWN to the
  nearest 10 before it is returned, so the function's *output* is privacy-safe
  by construction (a caller can never publish an exact vault size through it).
- `render_benchmark_report(...)` — plain aggregate data in, markdown string out.
  It accepts NO vault path and NO query text (see the contract note on the
  function): it renders only per-mode aggregate metrics/latency, rounded corpus
  counts, and methodology `meta` lines, so it is structurally incapable of
  leaking golden query text or vault-relative paths — it is never given them.

Scoping conventions (chosen to match how the rest of the codebase already scopes
markdown vs. media, not invented here):

- "files"  = total markdown pages across the WHOLE vault — the full-vault `.md`
             walk `vault.walk_vault_md` uses (skips `vault.VAULT_SCAN_SKIP_DIRS`,
             `.md`-only, Obsidian `.sync-conflict-` duplicates excluded).
- "notes"  = markdown pages in the KB scope — the KB walk `find()` indexes over
             (`find._walk_md` under `Knowledge Base/`, skipping
             `find.EXCLUDED_DIR_NAMES` such as `_Schema`; `.md`-only, sync-
             conflict excluded). Always a subset of "files".
- "media"  = binary artifacts (audio / video / image / pdf) anywhere in the
             vault outside the skip dirs. Extensions mirror `extract.py`'s
             `_AUDIO_EXTS | _VIDEO_EXTS | _IMAGE_EXTS | _PDF_EXTS`; they are
             inlined here (not imported) because `extract.py` pulls the GPU
             extraction stack, which would break this module's torch-free
             contract.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from .find import EXCLUDED_DIR_NAMES
from .vault import VAULT_SCAN_SKIP_DIRS, kb_root

# Media extensions — mirrors extract.py's audio/video/image/pdf buckets. Inlined
# rather than imported: extract.py imports the extraction/accel stack (torch),
# and this module must stay pure. Keep in sync with extract.py by hand.
_MEDIA_EXTS = frozenset({
    # audio
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".aac", ".wma", ".opus",
    # video
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".mpeg", ".mpg",
    # image
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic",
    # pdf
    ".pdf",
})

_ROUND_TO = 10


def _round_down(n: int) -> int:
    """Round a count DOWN to the nearest 10 (privacy floor: exact size never leaks)."""
    return (n // _ROUND_TO) * _ROUND_TO


def _iter_files(root: Path, skip_dirs: frozenset[str]) -> Iterable[Path]:
    """Yield every file under `root`, pruning any directory named in `skip_dirs`."""
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in place so os.walk does not descend into skipped subtrees.
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            yield Path(dirpath) / name


def count_corpus_stats(vault_root: Path) -> dict[str, int]:
    """Return rounded `{"files", "notes", "media"}` counts for `vault_root`.

    Privacy contract: every returned count is rounded DOWN to the nearest 10, so
    this function's output can be published as-is without revealing an exact
    vault size. It takes only a vault root and returns only integers — no path,
    filename, or content ever escapes it.

    Definitions (see the module docstring for the matching codebase conventions):
    - files: all `.md` across the whole vault (skips VAULT_SCAN_SKIP_DIRS).
    - notes: `.md` in the KB scope find() indexes (skips EXCLUDED_DIR_NAMES).
    - media: audio/video/image/pdf binaries anywhere outside the skip dirs.
    """
    files = 0
    media = 0
    for path in _iter_files(vault_root, VAULT_SCAN_SKIP_DIRS):
        ext = path.suffix.lower()
        if ext == ".md":
            if ".sync-conflict-" not in path.name:
                files += 1
        elif ext in _MEDIA_EXTS:
            media += 1

    notes = 0
    for path in _iter_files(kb_root(vault_root), EXCLUDED_DIR_NAMES):
        if path.suffix.lower() == ".md" and ".sync-conflict-" not in path.name:
            notes += 1

    return {
        "files": _round_down(files),
        "notes": _round_down(notes),
        "media": _round_down(media),
    }


# Ordered so the published table always lists the same columns in the same order.
_METRIC_COLUMNS: tuple[tuple[str, str], ...] = (
    ("NDCG@5", "ndcg5"),
    ("NDCG@10", "ndcg10"),
    ("MRR", "mrr"),
    ("recall@10", "recall10"),
)
_LATENCY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("latency median (ms)", "latency_median_ms"),
    ("latency p90 (ms)", "latency_p90_ms"),
)

# Rendered in this order when present in `meta`; unknown extra keys are appended.
_META_FIELDS: tuple[tuple[str, str], ...] = (
    ("exomem version", "exomem_version"),
    ("Embedding model", "embedding_model"),
    ("Reranker model", "reranker_model"),
    ("Hardware", "hardware"),
    ("Measured", "date"),
)


def render_benchmark_report(
    *,
    corpus: dict,
    per_mode: dict,
    golden_n: int,
    meta: dict,
) -> str:
    """Render an aggregate-only benchmark report as a markdown string.

    Contract — NO leak-capable inputs: this function accepts no vault path and
    no query text. `per_mode` is
    `{mode: {"ndcg5", "ndcg10", "mrr", "recall10",
             "latency_median_ms", "latency_p90_ms"}}` (aggregate floats only),
    `corpus` is `{"files", "notes", "media"}` (already rounded by
    `count_corpus_stats`), and `meta` carries methodology strings only (version,
    model names, hardware, date). Do NOT widen this signature to accept paths,
    per-query rows, or excerpts — the aggregate-only publication guarantee is
    structural precisely because leak-capable data is never passed in. A privacy
    regression test greps rendered output against every golden query and path.

    The date is passed in via `meta["date"]` — this function never calls
    `datetime.now()`, so rendering is deterministic for a given input.
    """
    lines: list[str] = []
    lines.append("## Retrieval benchmark")
    lines.append("")

    meta_lines = _render_meta(meta)
    if meta_lines:
        lines.extend(meta_lines)
        lines.append("")

    files = corpus.get("files", 0)
    notes = corpus.get("notes", 0)
    media = corpus.get("media", 0)
    lines.append(
        f"- Corpus scale: {files} markdown files, {notes} KB notes, "
        f"{media} media artifacts (counts rounded down to the nearest 10 for privacy)."
    )
    lines.append(f"- Golden set: {golden_n} queries.")
    lines.append("")

    header = ["Mode", *(h for h, _ in _METRIC_COLUMNS), *(h for h, _ in _LATENCY_COLUMNS)]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for mode, row in per_mode.items():
        cells = [str(mode)]
        for _, key in _METRIC_COLUMNS:
            cells.append(f"{float(row.get(key, 0.0)):.4f}")
        for _, key in _LATENCY_COLUMNS:
            cells.append(f"{float(row.get(key, 0.0)):.1f}")
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("### Limitations")
    lines.append("")
    lines.append(
        f"These figures come from a small, single-vault, self-graded golden set "
        f"({golden_n} queries, one private vault, relevance labels chosen by the "
        f"vault owner) — not an independent third-party benchmark. Latency is host- "
        f"and GPU-dependent and will differ on other hardware; treat it as a "
        f"reproducible self-measurement, not a portable guarantee."
    )
    lines.append("")

    return "\n".join(lines)


def _render_meta(meta: dict) -> list[str]:
    """Render known `meta` fields in a stable order, then any extra string keys."""
    out: list[str] = []
    shown: set[str] = set()
    for label, key in _META_FIELDS:
        if key in meta and meta[key] is not None:
            out.append(f"- {label}: {meta[key]}")
            shown.add(key)
    for key, value in meta.items():
        if key not in shown and value is not None:
            out.append(f"- {key}: {value}")
    return out
