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


def model_spec(kind: str) -> dict[str, Any]:
    """Read deployment choices from data without importing a model runtime."""
    spec = json.loads(files("exomem").joinpath("media_models.json").read_text(encoding="utf-8"))[kind]
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
