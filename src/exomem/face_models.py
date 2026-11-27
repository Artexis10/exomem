"""Face detection and embedding on ONNX Runtime: YuNet finds faces and SFace embeds them.

Both graphs are pinned media artifacts (`face-detect` and `face-embed` in `media_models.json`). They are
acquired through the served-model lifecycle and load only when a caller calls `load`, which the face step
does only while faces are on. Every calibration value comes from the manifest entry.

The decode, the non-maximum suppression and the alignment follow OpenCV's `FaceDetectorYN` and
`FaceRecognizerSF`. The artifact build compares them with OpenCV on fixture images
(`scripts/build-media-artifacts.py --compare`), so OpenCV is never a run-time dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import accel, media_models, runtime_resources

DETECT = "face-detect"
EMBED = "face-embed"
#: The smallest positive float32. OpenCV's similarity transform counts a singular value as nonzero above
#: twice this times the largest one.
_FLT_MIN = float(np.finfo(np.float32).tiny)
#: The fewest crop pixels per picture pixel at which the bilinear warp still reads every picture pixel.
_MIN_REACH = 0.5


class FacesUnavailable(RuntimeError):
    """The face runtime or a pinned face model cannot run on this host."""


def embedder_model_id() -> str:
    """The id that each face row records for its vector: the embedder and its artifact digest.

    It reads the manifest only, so a caller can compare stored rows with the pinned model without loading it.
    """
    spec = media_models.model_spec(EMBED)
    return f"{spec['model']}@{spec['artifact']['digest'][:16]}"


@dataclass(frozen=True)
class Detection:
    """One face in pixels of the array the detector saw: box (x, y, width, height), five landmarks, score."""

    box: tuple[float, float, float, float]
    landmarks: tuple[tuple[float, float], ...]
    score: float


@dataclass(frozen=True)
class Face:
    """One detected and embedded face, in pixels of the picture that `FaceModels.faces` received."""

    box: tuple[float, float, float, float]
    score: float
    vector: np.ndarray

    def region(self, width: int, height: int) -> tuple[float, float, float, float]:
        """The box as `read_media` takes a region: [x, y, width, height] fractions of the picture, clipped."""
        x, y, w, h = self.box
        left, top = min(max(x / width, 0.0), 1.0), min(max(y / height, 0.0), 1.0)
        right, bottom = min(max((x + w) / width, 0.0), 1.0), min(max((y + h) / height, 0.0), 1.0)
        return left, top, right - left, bottom - top


class FaceDetector:
    """YuNet at the picture's own aspect: the graph's height and width are symbolic."""

    def __init__(self, session: Any, spec: dict[str, Any]) -> None:
        self._session = session
        self._input = spec["input_name"]
        self._strides = tuple(int(stride) for stride in spec["strides"])
        self._divisor = int(spec["input_divisor"])
        self._long_side = int(spec["working_long_side"])
        # The four heads are YuNet's architecture (class, objectness, box, landmarks), one output per stride.
        self._names = [name for head in ("cls", "obj", "bbox", "kps") for name in spec["outputs"][head]]
        calibration = spec["calibration"]
        self._score_threshold = float(calibration["score_threshold"]["value"])
        self._nms_threshold = float(calibration["nms_threshold"]["value"])

    def working_pixels(self, image: Any) -> tuple[np.ndarray, float]:
        """The picture as RGB uint8 pixels no longer than the working side, and the scale applied."""
        from PIL import Image

        rgb = image.convert("RGB")
        width, height = rgb.size
        scale = min(1.0, self._long_side / max(width, height))
        if scale < 1.0:
            size = (max(1, round(width * scale)), max(1, round(height * scale)))
            rgb = rgb.resize(size, resample=Image.Resampling.BILINEAR)
        return np.asarray(rgb, dtype=np.uint8), scale

    def detect_pixels(self, pixels: np.ndarray) -> list[Detection]:
        """Faces in RGB uint8 pixels, as `FaceDetectorYN.detect` finds them in the same pixels."""
        height, width = pixels.shape[:2]
        padded_height = math.ceil(height / self._divisor) * self._divisor
        padded_width = math.ceil(width / self._divisor) * self._divisor
        # OpenCV feeds BGR values 0..255, padded with zeros on the right and the bottom.
        blob = np.zeros((1, 3, padded_height, padded_width), dtype=np.float32)
        blob[0, :, :height, :width] = pixels[:, :, ::-1].transpose(2, 0, 1)
        with runtime_resources.model_execution():  # Takes its turn with every other model call in the process.
            outputs = dict(zip(self._names, self._session.run(self._names, {self._input: blob}), strict=True))
        count = len(self._strides)
        boxes, landmarks, scores = [], [], []
        for index, stride in enumerate(self._strides):
            cls, obj, bbox, kps = (outputs[self._names[index + count * head]] for head in range(4))
            score = np.sqrt(np.clip(cls.reshape(-1), 0, 1) * np.clip(obj.reshape(-1), 0, 1))
            anchors = np.flatnonzero(score >= self._score_threshold)
            columns = padded_width // stride
            row = (anchors // columns).astype(np.float32)
            column = (anchors % columns).astype(np.float32)
            box = bbox.reshape(-1, 4)[anchors]
            points = kps.reshape(-1, 10)[anchors]
            size_x, size_y = np.exp(box[:, 2]) * stride, np.exp(box[:, 3]) * stride
            boxes.append(np.stack([
                (column + box[:, 0]) * stride - size_x / 2, (row + box[:, 1]) * stride - size_y / 2, size_x, size_y,
            ], axis=1))
            points = points.reshape(-1, 5, 2)
            landmarks.append(np.stack([
                (points[:, :, 0] + column[:, None]) * stride, (points[:, :, 1] + row[:, None]) * stride,
            ], axis=2))
            scores.append(score[anchors])
        all_boxes = np.concatenate(boxes)
        all_landmarks = np.concatenate(landmarks)
        all_scores = np.concatenate(scores)
        kept = _suppress(all_boxes, all_scores, self._score_threshold, self._nms_threshold)
        return [
            Detection(
                tuple(float(value) for value in all_boxes[index]),
                tuple((float(x), float(y)) for x, y in all_landmarks[index]),
                float(all_scores[index]),
            )
            for index in kept
        ]


def _suppress(boxes: np.ndarray, scores: np.ndarray, score_threshold: float, nms_threshold: float) -> list[int]:
    """OpenCV's `NMSBoxes` over integer rectangles: the kept indices, best score first.

    A single candidate is kept as it is, as `FaceDetectorYN` does. Candidates are a few dozen after the
    score threshold, so the pairwise loop stays small.
    """
    if len(scores) <= 1:
        return list(range(len(scores)))
    rectangles = np.trunc(boxes).astype(np.int64)  # OpenCV builds `Rect2i` by truncating each value
    order = [index for index in np.argsort(-scores, kind="stable") if scores[index] > score_threshold]
    kept: list[int] = []
    for index in order:
        x, y, w, h = rectangles[index]
        if all(_overlap((x, y, w, h), rectangles[other]) <= nms_threshold for other in kept):
            kept.append(int(index))
    return kept


def _overlap(first: Any, second: Any) -> float:
    """Intersection over union of two integer rectangles, as OpenCV's `rectOverlap`."""
    x1, y1, w1, h1 = (int(value) for value in first)
    x2, y2, w2, h2 = (int(value) for value in second)
    width = min(x1 + w1, x2 + w2) - max(x1, x2)
    height = min(y1 + h1, y2 + h2) - max(y1, y2)
    inter = width * height if width > 0 and height > 0 else 0
    union = w1 * h1 + w2 * h2 - inter
    return inter / union if union > 0 else 0.0


def similarity_transform(landmarks: np.ndarray, template: np.ndarray) -> np.ndarray | None:
    """The 2 × 3 similarity transform that maps five landmarks onto the template, as `FaceRecognizerSF`.

    It is Umeyama's least-squares estimate. Landmarks that all coincide have no transform: None.
    """
    source = np.asarray(landmarks, dtype=np.float64)
    target = np.asarray(template, dtype=np.float64)
    source_mean, target_mean = source.mean(axis=0), target.mean(axis=0)
    source_centered, target_centered = source - source_mean, target - target_mean
    variance = float((source_centered ** 2).sum() / len(source))
    if variance <= 0:
        return None
    covariance = target_centered.T @ source_centered / len(source)
    signs = np.array([1.0, 1.0 if np.linalg.det(covariance) >= 0 else -1.0])
    left, singular, right = np.linalg.svd(covariance)
    rank = int(np.count_nonzero(singular > singular.max() * 2 * _FLT_MIN))
    if rank == 1 and np.linalg.det(left) * np.linalg.det(right) <= 0:
        rotation = left @ np.diag([1.0, -1.0]) @ right
    elif rank == 1:
        rotation = left @ right
    else:
        rotation = left @ np.diag(signs) @ right
    scale = float(singular @ signs) / variance
    linear = rotation * scale
    return np.hstack([linear, (target_mean - linear @ source_mean)[:, None]])


def aligned_crop(pixels: np.ndarray, transform: np.ndarray, size: int) -> np.ndarray:
    """Warp RGB uint8 pixels by `transform` into a `size` × `size` crop, as OpenCV's bilinear `warpAffine`.

    Each crop pixel samples the picture at the inverse-mapped point; neighbours outside the picture read 0.
    """
    inverse = np.linalg.inv(transform[:, :2])
    offset = -inverse @ transform[:, 2]
    rows, columns = np.mgrid[0:size, 0:size].astype(np.float64)
    source_x = inverse[0, 0] * columns + inverse[0, 1] * rows + offset[0]
    source_y = inverse[1, 0] * columns + inverse[1, 1] * rows + offset[1]
    left, top = np.floor(source_x).astype(np.int64), np.floor(source_y).astype(np.int64)
    across, down = (source_x - left)[..., None], (source_y - top)[..., None]
    height, width = pixels.shape[:2]

    def sample(y: np.ndarray, x: np.ndarray) -> np.ndarray:
        inside = ((x >= 0) & (x < width) & (y >= 0) & (y < height))[..., None]
        return np.where(inside, pixels[np.clip(y, 0, height - 1), np.clip(x, 0, width - 1)], 0).astype(np.float64)

    value = (
        (1 - down) * ((1 - across) * sample(top, left) + across * sample(top, left + 1))
        + down * ((1 - across) * sample(top + 1, left) + across * sample(top + 1, left + 1))
    )
    return np.clip(np.rint(value), 0, 255).astype(np.uint8)


class FaceEmbedder:
    """SFace on an aligned crop: one unit vector per face."""

    def __init__(self, session: Any, spec: dict[str, Any]) -> None:
        self._session = session
        self._input = spec["input_name"]
        self._output = spec["output_name"]
        self._size = int(spec["crop_size"])
        self._width = int(spec["dimensions"])
        self._template = np.asarray(spec["calibration"]["alignment_template"]["value"], dtype=np.float64)

    def crop(self, pixels: np.ndarray, detection: Detection) -> np.ndarray | None:
        transform = similarity_transform(np.asarray(detection.landmarks), self._template)
        return None if transform is None else aligned_crop(pixels, transform, self._size)

    def smoothed_crop(self, picture: Any, detection: Detection) -> np.ndarray | None:
        """`crop` from a PIL picture, shrinking a large face's region first so the bilinear warp cannot alias.

        The warp reads 4 pixels per crop pixel; a face more than twice the crop's size would skip the others,
        so its region is first reduced, with Pillow's area-weighted filter, to twice the crop's size.
        """
        from PIL import Image

        transform = similarity_transform(np.asarray(detection.landmarks), self._template)
        if transform is None:
            return None
        reach = math.sqrt(abs(np.linalg.det(transform[:, :2])))  # Crop pixels per picture pixel.
        if reach >= _MIN_REACH:
            return aligned_crop(np.asarray(picture, dtype=np.uint8), transform, self._size)
        # The picture area the crop reads: its corners mapped back, with room for the filter's support.
        inverse = np.linalg.inv(transform[:, :2])
        corners = (np.array([[0, 0], [self._size, 0], [0, self._size], [self._size, self._size]], dtype=np.float64)
                   - transform[:, 2]) @ inverse.T
        margin = math.ceil(1 / reach)
        left, top = (max(0, math.floor(value) - margin) for value in corners.min(axis=0))
        right = min(picture.width, math.ceil(corners[:, 0].max()) + margin)
        bottom = min(picture.height, math.ceil(corners[:, 1].max()) + margin)
        if right <= left or bottom <= top:
            return aligned_crop(np.asarray(picture, dtype=np.uint8), transform, self._size)
        factor = reach / _MIN_REACH
        size = (max(1, round((right - left) * factor)), max(1, round((bottom - top) * factor)))
        region = picture.crop((left, top, right, bottom)).resize(size, resample=Image.Resampling.BOX)
        across, down = size[0] / (right - left), size[1] / (bottom - top)
        landmarks = [((x - left) * across, (y - top) * down) for x, y in detection.landmarks]
        return self.crop(np.asarray(region, dtype=np.uint8), Detection(detection.box, tuple(landmarks), detection.score))

    def embed_crop(self, crop: np.ndarray) -> np.ndarray | None:
        """The unit vector of an aligned RGB crop, or None when the model returns no usable vector."""
        # OpenCV feeds the aligned crop as RGB values 0..255 (`blobFromImage` with swapRB).
        blob = crop.astype(np.float32).transpose(2, 0, 1)[None]
        with runtime_resources.model_execution():
            vector = np.asarray(self._session.run([self._output], {self._input: blob})[0], dtype=np.float32)
        if vector.shape != (1, self._width) or not np.isfinite(vector).all():
            return None
        norm = float(np.linalg.norm(vector[0]))
        return vector[0] / norm if norm > 0 else None


class FaceModels:
    """Both face models, loaded together: `faces` turns one picture into embedded faces."""

    def __init__(self, detector: FaceDetector, embedder: FaceEmbedder) -> None:
        self.detector = detector
        self.embedder = embedder

    def faces(self, image: Any) -> list[Face]:
        """Every face in an upright PIL picture, with its box in that picture's pixels and its vector.

        The detector sees the picture at its working size; the embedder crops each face from the picture's
        own pixels, so a large picture's face keeps the detail that the downscale dropped.
        """
        rgb = image.convert("RGB")
        pixels, scale = self.detector.working_pixels(rgb)
        found = []
        for seen in self.detector.detect_pixels(pixels):
            detection = Detection(
                tuple(value / scale for value in seen.box),
                tuple((x / scale, y / scale) for x, y in seen.landmarks),
                seen.score,
            )
            crop = self.embedder.smoothed_crop(rgb, detection)
            vector = None if crop is None else self.embedder.embed_crop(crop)
            if vector is not None:
                found.append(Face(detection.box, detection.score, vector))
        return found


def load() -> FaceModels:
    """Acquire both pinned graphs and open their sessions; `FacesUnavailable` when either cannot run.

    The sessions take a GPU execution provider when `accel` picks a GPU device and fall back to the CPU.
    CUDA is avoided while speech runs in the same process, for the cuDNN clash that `embeddings._clip_device`
    describes: the face step runs in the media worker beside it.
    """
    try:
        import onnxruntime as ort
        import PIL  # noqa: F401 -- the detector decodes pictures through Pillow

        providers = accel.onnx_providers(accel.select_device(avoid_cuda_when_asr=True))
        sessions = []
        for kind in (DETECT, EMBED):
            artifact = media_models.ensure_media_model(kind)
            options = ort.SessionOptions()
            runtime_resources.configure_onnx_media_session_options(options, default_threads=2)
            session = ort.InferenceSession(
                str(artifact.root / artifact.spec["graph_file"]), sess_options=options, providers=providers,
            )
            sessions.append((session, artifact.spec))
    except ImportError as error:
        raise FacesUnavailable(f"the face runtime is not installed: {error.name}") from error
    except Exception as error:  # noqa: BLE001 -- any acquisition or load failure leaves faces unavailable
        raise FacesUnavailable(f"a face model cannot load: {error}") from error
    (detect_session, detect_spec), (embed_session, embed_spec) = sessions
    return FaceModels(FaceDetector(detect_session, detect_spec), FaceEmbedder(embed_session, embed_spec))
