"""The CLIP image tower as a torch-free ONNX build in the existing vector space."""

from __future__ import annotations

import json

import numpy as np

from . import runtime_resources
from .clip_index import CLIP_DIM


class OnnxClipImageEncoder:
    """Run the pinned vision tower and its projection on CPU, as stored vectors expect."""

    def __init__(self) -> None:
        import onnxruntime as ort

        from .media_models import ensure_media_model

        artifact = ensure_media_model("image")
        spec = artifact.spec
        if spec["dimensions"] != CLIP_DIM:
            raise ValueError("the image model does not match the image vector width")
        config = json.loads(
            (artifact.root / spec["preprocessor_file"]).read_text(encoding="utf-8")
        )
        self._size = int(config["size"])
        self._crop = int(config["crop_size"])
        self._resample = int(config["resample"])
        self._mean = np.asarray(config["image_mean"], dtype=np.float32).reshape(3, 1, 1)
        self._std = np.asarray(config["image_std"], dtype=np.float32).reshape(3, 1, 1)
        self._input_name = spec["input_name"]
        self._output_name = spec["output_name"]
        options = ort.SessionOptions()
        runtime_resources.configure_onnx_media_session_options(options, default_threads=2)
        self._session = ort.InferenceSession(
            str(artifact.root / spec["graph_file"]),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )

    def _pixels(self, image) -> np.ndarray:
        # The reference processor: shortest edge to `size` (long edge floored),
        # one bicubic PIL resize, centre crop, rescale and normalize. No EXIF
        # transpose, because the stored vectors were made without one.
        image = image.convert("RGB")
        width, height = image.size
        short, long = (width, height) if width <= height else (height, width)
        scaled = int(self._size * long / short)
        target = (self._size, scaled) if width <= height else (scaled, self._size)
        resized = np.asarray(image.resize(target, resample=self._resample), dtype=np.uint8)
        top = (resized.shape[0] - self._crop) // 2
        left = (resized.shape[1] - self._crop) // 2
        cropped = resized[top:top + self._crop, left:left + self._crop]
        pixels = cropped.astype(np.float32).transpose(2, 0, 1) * np.float32(1 / 255)
        return (pixels - self._mean) / self._std

    def encode(self, images, convert_to_numpy: bool = True, normalize_embeddings: bool = True):
        """Return float32 image vectors; one PIL image in, one vector out."""
        single = not isinstance(images, (list, tuple))
        batch = [images] if single else list(images)
        vectors = []
        for image in batch:
            output = self._session.run(
                [self._output_name], {self._input_name: self._pixels(image)[None]}
            )[0]
            vector = np.asarray(output, dtype=np.float32)
            if vector.shape != (1, CLIP_DIM) or not np.isfinite(vector).all():
                raise ValueError("the image model returned an invalid image vector")
            if normalize_embeddings:
                norm = float(np.linalg.norm(vector[0]))
                if not np.isfinite(norm) or norm == 0:
                    raise ValueError("the image model returned an empty image vector")
                vector = vector / norm
            vectors.append(vector[0])
        result = np.asarray(vectors, dtype=np.float32).reshape(-1, CLIP_DIM)
        return result[0] if single else result
