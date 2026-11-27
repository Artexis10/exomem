"""Build published media artifacts from the pinned exports; never run in a cell."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 << 20):
            digest.update(block)
    return digest.hexdigest()


#: Earlier releases write every If-branch initializer twice when they save an
#: optimized model with external initializers, and the saved model does not load.
OFFLINE_OPTIMIZER_MIN = (1, 31)


def optimize_offline(graph: Path) -> None:
    """Save the graph with its constants folded at basic level, as file-backed initializers.

    ONNX Runtime otherwise folds them at load into private memory in every process;
    saved folded, they map from the file and all processes share them. Basic level
    holds only hardware-independent rewrites, so any CPU can load the result.
    """
    import onnxruntime as ort

    if tuple(int(part) for part in ort.__version__.split(".")[:2]) < OFFLINE_OPTIMIZER_MIN:
        raise SystemExit(f"offline optimization needs onnxruntime>=1.31; this is {ort.__version__}")
    staging = graph.parent / ".offline-optimized"
    staging.mkdir()
    data = graph.with_suffix(".data").name
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    options.optimized_model_filepath = str(staging / graph.name)
    options.add_session_config_entry("session.optimized_model_external_initializers_file_name", data)
    options.add_session_config_entry("session.optimized_model_external_initializers_min_size_in_bytes", "1024")
    session = ort.InferenceSession(str(graph), sess_options=options, providers=["CPUExecutionProvider"])
    del session
    shutil.move(staging / graph.name, graph)
    shutil.move(staging / data, graph.parent / data)
    staging.rmdir()


FACE_KINDS = ("face-detect", "face-embed")


def build_face_graph(source: Path, spec: dict, root: Path) -> None:
    """Rewrite the pinned OpenCV Zoo graph with its weights unchanged and check the shapes the manifest records.

    The initializers leave the graph inputs, so ONNX Runtime folds them as constants. The published detector
    has a fixed 640 × 640 input; its height, width and anchor counts become symbolic, so it runs at each
    picture's own size, as OpenCV's `FaceDetectorYN` runs it.
    """
    import onnx
    import onnxruntime as ort

    model = onnx.load(source / spec["source"]["graph_file"])
    for index, tensor in enumerate(model.graph.initializer):
        # The embedder stores its weights as typed fields; ONNX externalizes only raw bytes, of the same values.
        if not tensor.raw_data:
            model.graph.initializer[index].CopyFrom(onnx.numpy_helper.from_array(onnx.numpy_helper.to_array(tensor), tensor.name))
    initializers = {tensor.name for tensor in model.graph.initializer}
    kept = [value for value in model.graph.input if value.name not in initializers]
    del model.graph.input[:]
    model.graph.input.extend(kept)
    if spec["recipe"].get("symbolic_dimensions"):
        # The export's intermediate shapes are fixed at 640 × 640; ONNX Runtime infers them again.
        del model.graph.value_info[:]
        for value in (*model.graph.input, *model.graph.output):
            for index, name in spec["recipe"]["symbolic_dimensions"].get(value.name, {}).items():
                value.type.tensor_type.shape.dim[int(index)].dim_param = name
    onnx.checker.check_model(model)
    onnx.save_model(model, root / spec["graph_file"], save_as_external_data=True, all_tensors_to_one_file=True,
                    location=spec["graph_file"] + ".data", size_threshold=1024)
    shutil.copyfile(source / spec["source"]["license_file"], root / "LICENSES.txt")
    session = ort.InferenceSession(str(root / spec["graph_file"]), providers=["CPUExecutionProvider"])
    shapes = {value.name: list(value.shape) for value in (*session.get_inputs(), *session.get_outputs())}
    if shapes != spec["shapes"]:
        raise ValueError(f"the built graph's shapes differ from the manifest: {json.dumps(shapes)}")


def compare_faces(kind: str, spec: dict, built: Path, source: Path, fixtures: Path, detector_source: Path | None) -> dict:
    """Build-time only: compare the run-time numpy pipeline with OpenCV's face classes on fixture images.

    The detector must find the faces OpenCV finds, with the same boxes, landmarks and scores. The embedder
    must align the same crops from OpenCV's detections and give the same vectors. OpenCV runs here only.
    """
    import cv2
    import numpy as np
    import onnxruntime as ort
    from PIL import Image

    from exomem import face_models, media_models

    def session(graph: Path):
        return ort.InferenceSession(str(graph), providers=["CPUExecutionProvider"])

    detecting = kind == "face-detect"
    detect_spec = spec if detecting else media_models.model_spec("face-detect")
    detector_folder = source if detecting else detector_source
    if detector_folder is None:
        raise SystemExit("comparing the embedder needs --detector-source, the pinned detector's source folder")
    upstream_detector = str(detector_folder / detect_spec["source"]["graph_file"])
    calibration = detect_spec["calibration"]
    if detecting:
        detector = face_models.FaceDetector(session(built / spec["graph_file"]), spec)
    else:
        embedder = face_models.FaceEmbedder(session(built / spec["graph_file"]), spec)
        recognizer = cv2.FaceRecognizerSF.create(str(source / spec["source"]["graph_file"]), "")
    limits = spec["recipe"]["comparison_limits"]
    worst = dict.fromkeys(limits, 0.0)
    images = []
    for path in sorted(item for item in fixtures.iterdir() if item.is_file()):
        with Image.open(path) as image:
            pixels = np.asarray(image.convert("RGB"), dtype=np.uint8)
        bgr = np.ascontiguousarray(pixels[:, :, ::-1])
        reference = cv2.FaceDetectorYN.create(
            upstream_detector, "", (pixels.shape[1], pixels.shape[0]),
            calibration["score_threshold"]["value"], calibration["nms_threshold"]["value"],
        )
        expected = reference.detect(bgr)[1]
        expected = np.zeros((0, 15), dtype=np.float32) if expected is None else expected
        images.append({"image": path.name, "faces": len(expected)})
        if detecting:
            found = detector.detect_pixels(pixels)
            if len(found) != len(expected):
                raise ValueError(f"{path.name}: {len(found)} faces against OpenCV's {len(expected)}")
            for face, row in zip(found, expected, strict=True):
                worst["box"] = max(worst["box"], float(np.abs(np.array(face.box) - row[:4]).max()))
                landmarks = np.array(face.landmarks).reshape(-1)
                worst["landmark"] = max(worst["landmark"], float(np.abs(landmarks - row[4:14]).max()))
                worst["score"] = max(worst["score"], abs(face.score - float(row[14])))
            continue
        for row in expected:
            detection = face_models.Detection(tuple(row[:4]), tuple(map(tuple, row[4:14].reshape(5, 2))), float(row[14]))
            crop = embedder.crop(pixels, detection)
            aligned = recognizer.alignCrop(bgr, row)
            worst["crop_mean"] = max(worst["crop_mean"], float(np.abs(crop.astype(np.float32) - aligned[:, :, ::-1]).mean()))
            theirs = recognizer.feature(aligned).reshape(-1)
            cosine = float(embedder.embed_crop(crop) @ (theirs / np.linalg.norm(theirs)))
            worst["cosine_gap"] = max(worst["cosine_gap"], 1.0 - cosine)
    report = {"opencv": cv2.__version__, "onnxruntime": ort.__version__, "images": images, "worst": worst}
    if any(worst[name] > limit for name, limit in limits.items()) or not any(entry["faces"] for entry in images):
        raise ValueError(f"the numpy pipeline differs from OpenCV beyond the recipe's limits: {json.dumps(report)}")
    return report


def pack(root: Path, spec: dict, destination: Path) -> dict:
    from exomem.embedding_backend import artifact_sha256, write_artifact_asset
    from exomem.media_models import artifact_spec

    served = artifact_spec(spec)
    identity = artifact_sha256(root, served.files)
    asset = write_artifact_asset(spec["model"], served, destination, source_dir=root)
    return {"digest": identity, "asset_digest": sha256(asset), "asset": asset.name}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["image", "image-query", "speech", *FACE_KINDS])
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--externalized", action="store_true", help="Reuse a previously qualified external-data build")
    parser.add_argument("--vad-source", type=Path)
    parser.add_argument("--compare", type=Path, help="Face kinds: fixture images to compare with OpenCV (build time only)")
    parser.add_argument("--detector-source", type=Path, help="face-embed --compare: the pinned detector's source folder")
    args = parser.parse_args()
    import onnx

    from exomem.media_models import check_entry

    spec = json.loads(args.registry.read_text())[args.kind]
    check_entry(args.kind, spec)
    inputs = (
        {name: spec["source_files"][name] for name in spec["metadata_files"]} | spec["external_files"]
        if args.externalized else spec["source_files"]
    )
    for name, expected in inputs.items():
        if sha256(args.source / name) != expected:
            raise ValueError(f"the pinned source file does not match: {name}")
    root = args.output / args.kind
    root.mkdir(parents=True, exist_ok=False)
    for name in spec["metadata_files"]:
        shutil.copyfile(args.source / name, root / name)
    if args.kind == "image":
        import torch
        from transformers import CLIPModel

        clip = CLIPModel.from_pretrained(args.source, local_files_only=True).eval()

        class ImageTower(torch.nn.Module):
            """Vision tower plus its trained projection, as sentence-transformers runs them."""

            def __init__(self, model):
                super().__init__()
                self.model = model

            def forward(self, pixel_values):
                return self.model.get_image_features(pixel_values=pixel_values)

        exported = args.output / "image-export.onnx"
        size = json.loads((args.source / spec["preprocessor_file"]).read_text())["crop_size"]
        with torch.no_grad():
            torch.onnx.export(
                ImageTower(clip), (torch.zeros(1, 3, size, size),), exported,
                input_names=[spec["input_name"]], output_names=[spec["output_name"]],
                dynamic_axes={spec["input_name"]: {0: "batch_size"}, spec["output_name"]: {0: "batch_size"}},
                opset_version=17, dynamo=False,
            )
        model = onnx.load(exported)
        onnx.save_model(model, root / "model.onnx", save_as_external_data=True, all_tensors_to_one_file=True, location="model.onnx.data", size_threshold=1024)
        exported.unlink()
        for leftover in args.output.glob("image-export.onnx*"):
            leftover.unlink()
    elif args.kind == "image-query":
        if args.externalized:
            model = onnx.load(args.source / "model.onnx", load_external_data=False)
            locations = set()
            for tensor in model.graph.initializer:
                for entry in tensor.external_data:
                    if entry.key == "location":
                        locations.add(entry.value)
                        entry.value = "model.onnx.data"
            if len(locations) != 1:
                raise ValueError("the qualified query build must use one weights file")
            shutil.copyfile(args.source / locations.pop(), root / "model.onnx.data")
            onnx.save_model(model, root / "model.onnx")
        else:
            import numpy as np
            from onnx import TensorProto, helper, numpy_helper
            from safetensors.numpy import load_file

            pooling = json.loads((args.source / "1_Pooling/config.json").read_text())
            dense = json.loads((args.source / "2_Dense/config.json").read_text())
            if not pooling["pooling_mode_mean_tokens"] or pooling["pooling_mode_cls_token"]:
                raise ValueError("the pinned query encoder must use masked mean pooling")
            if dense["activation_function"] != "torch.nn.modules.linear.Identity" or dense["bias"]:
                raise ValueError("the pinned query projection must be linear without bias")
            model = onnx.load(args.source / "onnx/model.onnx")
            if next(x.version for x in model.opset_import if x.domain == "") != 11:
                raise ValueError("this recipe targets the pinned opset 11 export")
            hidden = model.graph.output[0].name
            prefix = "query_projection_"
            weight = load_file(args.source / "2_Dense/model.safetensors")["linear.weight"]
            model.graph.initializer.extend([
                numpy_helper.from_array(np.array(1e-9, dtype=np.float32), prefix + "minimum"),
                numpy_helper.from_array(weight.T.copy(), prefix + "weight"),
            ])
            model.graph.node.extend([
                helper.make_node("Cast", ["attention_mask"], [prefix + "mask"], to=TensorProto.FLOAT),
                helper.make_node("Unsqueeze", [prefix + "mask"], [prefix + "expanded_mask"], axes=[2]),
                helper.make_node("Mul", [hidden, prefix + "expanded_mask"], [prefix + "masked"]),
                helper.make_node("ReduceSum", [prefix + "masked"], [prefix + "sum"], axes=[1], keepdims=0),
                helper.make_node("ReduceSum", [prefix + "mask"], [prefix + "count"], axes=[1], keepdims=1),
                helper.make_node("Clip", [prefix + "count", prefix + "minimum"], [prefix + "denominator"]),
                helper.make_node("Div", [prefix + "sum", prefix + "denominator"], [prefix + "mean"]),
                helper.make_node("MatMul", [prefix + "mean", prefix + "weight"], [spec["output_name"]]),
            ])
            del model.graph.output[:]
            model.graph.output.extend([helper.make_tensor_value_info(spec["output_name"], TensorProto.FLOAT, ["batch_size", spec["dimensions"]])])
            onnx.save_model(model, root / "model.onnx", save_as_external_data=True, all_tensors_to_one_file=True, location="model.onnx.data", size_threshold=1024)
    elif args.kind in FACE_KINDS:
        build_face_graph(args.source, spec, root)
        if args.compare is not None:
            comparison = compare_faces(args.kind, spec, root, args.source, args.compare, args.detector_source)
    else:
        for name in spec["graphs"]:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if args.externalized:
                shutil.copyfile(args.source / name, target)
                shutil.copyfile((args.source / name).with_suffix(".data"), target.with_suffix(".data"))
            else:
                model = onnx.load(args.source / name, load_external_data=False)
                onnx.save_model(model, target, save_as_external_data=True, all_tensors_to_one_file=True, location=target.with_suffix(".data").name, size_threshold=1024)
        for name in spec["recipe"].get("offline_optimized", []):
            optimize_offline(root / name)
        if args.vad_source is None:
            raise ValueError("speech requires its pinned VAD source")
        for name, expected in spec["source"]["vad"]["files"].items():
            if sha256(args.vad_source / name) != expected:
                raise ValueError(f"the pinned VAD file does not match: {name}")
        (root / "vad").mkdir()
        shutil.copyfile(args.vad_source / "config.json", root / "vad/config.json")
        model = onnx.load(args.vad_source / spec["source"]["vad"]["graph_file"], load_external_data=False)
        onnx.save_model(model, root / "vad/silero_vad.onnx", save_as_external_data=True, all_tensors_to_one_file=True, location="silero_vad.data", size_threshold=1024)
    provenance = {
        "model": spec["model"], "revision": spec["artifact"]["revision"],
        "source": spec["source"], "recipe": spec["recipe"],
        "qualified_external_input": args.externalized, "onnx": onnx.__version__,
        **({"torch": __import__("torch").__version__, "transformers": __import__("transformers").__version__} if args.kind == "image" else {}),
        **({"onnxruntime": __import__("onnxruntime").__version__} if spec["recipe"].get("offline_optimized") else {}),
        "recipe_sha256": sha256(Path(__file__)),
    }
    (root / "source.json").write_text(json.dumps(provenance, sort_keys=True, indent=2) + "\n")
    result = pack(root, spec, args.output)
    if args.compare is not None and args.kind in FACE_KINDS:
        result["comparison"] = comparison
    (args.output / f"{args.kind}-identity.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
