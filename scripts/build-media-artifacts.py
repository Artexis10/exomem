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


def pack(root: Path, spec: dict, destination: Path) -> dict:
    from exomem.embedding_backend import artifact_sha256, write_artifact_asset
    from exomem.media_models import artifact_spec

    served = artifact_spec(spec)
    identity = artifact_sha256(root, served.files)
    asset = write_artifact_asset(spec["model"], served, destination, source_dir=root)
    return {"digest": identity, "asset_digest": sha256(asset), "asset": asset.name}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["image", "image-query", "speech"])
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--externalized", action="store_true", help="Reuse a previously qualified external-data build")
    parser.add_argument("--vad-source", type=Path)
    args = parser.parse_args()
    import onnx

    spec = json.loads(args.registry.read_text())[args.kind]
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
        "recipe_sha256": sha256(Path(__file__)),
    }
    (root / "source.json").write_text(json.dumps(provenance, sort_keys=True, indent=2) + "\n")
    result = pack(root, spec, args.output)
    (args.output / f"{args.kind}-identity.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
