"""Pinned media artifacts use the existing served-model acquisition lifecycle."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import embedding_backend


@dataclass(frozen=True)
class MediaModel:
    root: Path
    spec: dict[str, Any]
    digest: str


def check_entry(kind: str, spec: dict[str, Any]) -> None:
    """Refuse a manifest entry that names no licence source for its weights.

    A model swap that drops its licence evidence then fails where the manifest is read and where the
    artifact is built, before any host acquires the bytes.
    """
    sources = spec.get("source", {}).get("license_sources")
    # A source is a URL a reviewer can open; the scheme prefix checks the field's format, not its meaning.
    if not sources or not all(isinstance(source, str) and source.startswith("https://") for source in sources):
        raise ValueError(f"{kind}: the media model manifest entry names no licence source")


def model_spec(kind: str) -> dict[str, Any]:
    """Read deployment choices from data without importing a model runtime."""
    spec = json.loads(files("exomem").joinpath("media_models.json").read_text(encoding="utf-8"))[kind]
    check_entry(kind, spec)
    return {**spec, "revision": spec["artifact"]["revision"]}


def artifact_spec(spec: dict[str, Any]) -> embedding_backend.ServedArtifact:
    artifact = dict(spec["artifact"])
    artifact["files"] = tuple(artifact["files"])
    return embedding_backend.ServedArtifact(**artifact)


def ensure_media_model(kind: str) -> MediaModel:
    """Fetch matching published bytes; new media artifacts never build on a host."""
    spec = model_spec(kind)
    artifact = artifact_spec(spec)
    _, digest = embedding_backend.ensure_artifact(spec["model"], artifact)
    root = embedding_backend.artifact_dir(spec["model"], artifact)
    return MediaModel(root=root, spec=spec, digest=digest)


def media_model_cached(kind: str) -> bool:
    """Report whether all pinned bytes are installed, without acquiring them."""
    spec = model_spec(kind)
    return embedding_backend.artifact_cached(spec["model"], artifact_spec(spec))
