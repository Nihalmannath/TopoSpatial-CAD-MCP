"""Run a reproducible Raster2Seq baseline using a private Windows runtime copy."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_SHA256 = "f7c0ef9379aa8cf11349da8b2179214e0d495af758383984a2361a1309aab3b7"


def replace_required(path, old, new):
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise ValueError(f"Upstream changed: expected patch context missing in {path}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def prepare_runtime(upstream, runtime):
    if (runtime / "runtime_manifest.json").exists():
        return runtime
    if runtime.exists():
        raise ValueError("Incomplete runtime exists; choose a fresh --runtime directory")
    def ignored(directory, names):
        excluded = {".git", "__pycache__", ".venv", "checkpoints", "save_models", "pred_outputs"}
        if Path(directory) == upstream:
            excluded.add("data")
        return set(names) & excluded
    shutil.copytree(upstream, runtime, ignore=ignored)
    functions = runtime / "models/ops/functions/ms_deform_attn_func.py"
    replace_required(functions, "import MultiScaleDeformableAttention as MSDA",
                     "try:\n    import MultiScaleDeformableAttention as MSDA\nexcept ImportError:\n    MSDA = None")
    module = runtime / "models/ops/modules/ms_deform_attn.py"
    replace_required(module, "from ..functions import MSDeformAttnFunction",
                     "from ..functions import MSDeformAttnFunction\nfrom ..functions.ms_deform_attn_func import MSDA, ms_deform_attn_core_pytorch")
    replace_required(module,
        "output = MSDeformAttnFunction.apply(\n            value, input_spatial_shapes, input_level_start_index, sampling_locations, attention_weights, self.im2col_step)",
        "if MSDA is None:\n            output = ms_deform_attn_core_pytorch(value, input_spatial_shapes.tolist(), sampling_locations, attention_weights)\n        else:\n            output = MSDeformAttnFunction.apply(\n                value, input_spatial_shapes, input_level_start_index, sampling_locations, attention_weights, self.im2col_step)")
    replace_required(runtime / "predict.py", 'torch.load(args.checkpoint, map_location="cpu")',
                     'torch.load(args.checkpoint, map_location="cpu", weights_only=False)')
    # Fail rather than run a partly initialized checkpoint silently.
    replace_required(runtime / "predict.py", 'model.load_state_dict(ckpt_state_dict, strict=False)',
                     'model.load_state_dict(ckpt_state_dict, strict=True)')
    replace_required(runtime / "util/plot_utils.py", "from matplotlib.cm import get_cmap",
                     "from matplotlib import colormaps\nget_cmap = colormaps.get_cmap")
    replace_required(runtime / "models/backbone.py", "pretrained=True", "pretrained=False")
    manifest = {"upstream": str(upstream), "attention": "upstream PyTorch reference fallback when native extension is unavailable",
                "changes": ["optional native import", "reference grid_sample attention", "trusted checkpoint deserialization", "strict state dictionary loading", "current matplotlib colormap API", "skip redundant ImageNet download before full checkpoint loading"],
                "upstream_predict_sha256": hashlib.sha256((upstream / "predict.py").read_bytes()).hexdigest()}
    (runtime / "runtime_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return runtime


def normalize_predictions(raw, image_size, model_size=256):
    width, height = image_size
    scale = min(model_size/height, model_size/width)
    resized_h, resized_w = int(height*scale), int(width*scale)
    left, top = (model_size-resized_w)//2, (model_size-resized_h)//2
    polygons = []
    for item in raw:
        coordinates = item["segmentation"]
        if coordinates and not isinstance(coordinates[0], list):
            coordinates = list(zip(coordinates[::2], coordinates[1::2]))
        points = [[(x-left)*width/resized_w, (y-top)*height/resized_h] for x,y in coordinates]
        polygons.append({"category_id":item["category_id"], "points":points})
    return {"coordinate_space":"original_image_pixels", "image_size":[width,height],
            "annotation_complete":False, "polygons_complete":False,
            "transform":{"model_size":model_size,"resized_size":[resized_w,resized_h],"padding_left_top":[left,top]},
            "polygons":polygons}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, default=Path.home()/"Documents/GitHub/Raster2Seq")
    parser.add_argument("--runtime", type=Path, default=ROOT/"training/raster2seq_runtime")
    parser.add_argument("--python", type=Path, default=ROOT/".venv-raster2seq/Scripts/python.exe")
    parser.add_argument("--checkpoint", type=Path, default=ROOT/"models/pretrained/raster2seq/checkpoint.pth")
    parser.add_argument("--image", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT/"outputs/raster2seq/zero_shot")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    runtime = prepare_runtime(args.upstream, args.runtime)
    if args.prepare_only:
        print(runtime)
        return
    digest = hashlib.file_digest(args.checkpoint.open("rb"), "sha256").hexdigest()
    if digest != CHECKPOINT_SHA256:
        raise SystemExit("Checkpoint does not match the verified upstream CubiCasa5K release")
    if args.image is None:
        pilot = json.loads((ROOT/"dataset/pilot_30.json").read_text())
        args.image = ROOT/"dataset/raw_233"/pilot["plans"][0]["filename"]
    args.output.mkdir(parents=True, exist_ok=True)
    inputs = args.output / "input"
    inputs.mkdir(exist_ok=True)
    if list(inputs.iterdir()):
        raise SystemExit("Output input folder is not empty; choose a new --output for each baseline")
    with Image.open(args.image) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    image.save(inputs / "baseline.png")
    command = [str(args.python), "-B", "-u", str(runtime/"predict.py"),
        "--dataset_name", "cubicasa", "--dataset_root", str(inputs),
        "--checkpoint", args.checkpoint.as_posix(), "--output_dir", str(args.output),
        "--semantic_classes", "12", "--input_channels", "3", "--poly2seq",
        "--seq_len", "512", "--num_bins", "32", "--disable_poly_refine",
        "--dec_attn_concat_src", "--per_token_sem_loss", "--use_anchor", "--ema4eval",
        "--save_pred", "--batch_size", "1", "--num_workers", "0"]
    (args.output/"run_manifest.json").write_text(json.dumps({"command":command,"source":str(args.image),
        "source_sha256":hashlib.sha256(args.image.read_bytes()).hexdigest(),"checkpoint_sha256":digest,
        "runtime":json.loads((runtime/"runtime_manifest.json").read_text())},indent=2))
    with (args.output/"inference.log").open("w",encoding="utf-8") as log:
        completed = subprocess.run(command, cwd=runtime, stdout=log, stderr=subprocess.STDOUT)
    if completed.returncode:
        raise SystemExit(f"Inference failed. See {args.output / 'inference.log'}")
    raw_path = args.output / args.checkpoint.parent.name / "jsons/baseline.json"
    normalized = normalize_predictions(json.loads(raw_path.read_text()), image.size)
    normalized["source_sha256"] = hashlib.sha256(args.image.read_bytes()).hexdigest()
    (args.output/"polygons_original_pixels.json").write_text(json.dumps(normalized,indent=2))
    overlay = image.convert("RGBA")
    draw = ImageDraw.Draw(overlay)
    for i,item in enumerate(normalized["polygons"]):
        points=[tuple(p) for p in item["points"]]
        if len(points)>=2:
            draw.line(points+[points[0]],fill=(230,50,50),width=3)
            draw.text(points[0],str(i+1),fill=(230,30,30))
    overlay.convert("RGB").save(args.output/"overlay_original.png")
    print(json.dumps({"polygons":len(normalized["polygons"]),"output":str(args.output),"reviewed":False}))


if __name__ == "__main__":
    main()
