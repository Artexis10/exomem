"""The multilingual query tower aligned to the stored CLIP image vectors."""

from __future__ import annotations

import json

import numpy as np

from . import runtime_resources
from .clip_index import CLIP_DIM


class OnnxClipQueryEncoder:
    """Run the complete query graph, including its trained projection, on CPU."""

    def __init__(self) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        from .media_models import ensure_media_model

        artifact = ensure_media_model("image-query")
        spec = artifact.spec
        if spec["dimensions"] != CLIP_DIM:
            raise ValueError("the query model does not match the image vector width")
        self._output_name = spec["output_name"]
        self._tokenizer = Tokenizer.from_file(str(artifact.root / spec["tokenizer_file"]))
        config = json.loads(
            (artifact.root / spec["tokenizer_config_file"]).read_text(encoding="utf-8")
        )
        pad_token = config["pad_token"]
        if isinstance(pad_token, dict):
            pad_token = pad_token["content"]
        pad_id = self._tokenizer.token_to_id(pad_token)
        if pad_id is None:
            raise ValueError("the query tokenizer does not contain its padding token")
        self._tokenizer.enable_truncation(max_length=spec["max_tokens"])
        self._tokenizer.enable_padding(pad_id=pad_id, pad_token=pad_token)
        options = ort.SessionOptions()
        runtime_resources.configure_onnx_media_session_options(options, default_threads=2)
        self._session = ort.InferenceSession(
            str(artifact.root / spec["graph_file"]),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )

    def encode(self, texts: list[str]) -> np.ndarray:
        """Return normalized float32 vectors without changing the image space."""
        vectors = []
        for text in texts:
            encoded = self._tokenizer.encode(text)
            # These are the input names and int64 types of the full query graph.
            inputs = {
                "input_ids": np.asarray([encoded.ids], dtype=np.int64),
                "attention_mask": np.asarray([encoded.attention_mask], dtype=np.int64),
            }
            vector = np.asarray(
                self._session.run([self._output_name], inputs)[0], dtype=np.float32
            )
            if vector.shape != (1, CLIP_DIM) or not np.isfinite(vector).all():
                raise ValueError("the query model returned an invalid image vector")
            norm = float(np.linalg.norm(vector[0]))
            if not np.isfinite(norm) or norm == 0:
                raise ValueError("the query model returned an empty image vector")
            vectors.append(vector[0] / norm)
        return np.asarray(vectors, dtype=np.float32).reshape(-1, CLIP_DIM)
