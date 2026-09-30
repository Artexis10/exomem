"""Local, identity-checked tokenization; no network or model-supplied Python."""
from __future__ import annotations

import hashlib
from pathlib import Path

from lme.metered_profiles import ModelProfile


def tokenizer_bytes(profile: ModelProfile, path: Path | None) -> bytes | None:
    if profile.tokenizer_sha256 is None:
        if path is not None:
            raise ValueError("this model does not accept a tokenizer artifact")
        return None
    if path is None or path.is_symlink() or not path.is_file():
        raise ValueError("model requires its pinned local tokenizer")
    # The official artifact is about 20 MB; bound reads before allocating.
    with path.open("rb") as stream:
        raw = stream.read(32_000_001)
    if len(raw) > 32_000_000 or hashlib.sha256(raw).hexdigest() != profile.tokenizer_sha256:
        raise ValueError("tokenizer digest differs from the model profile")
    return raw


def load_tokenizer(profile: ModelProfile, path: Path | None):
    raw = tokenizer_bytes(profile, path)
    if raw is None:
        return None
    from tokenizers import Tokenizer

    encoder = Tokenizer.from_str(raw.decode("utf-8"))
    encoder.no_truncation()
    encoder.no_padding()
    return encoder
