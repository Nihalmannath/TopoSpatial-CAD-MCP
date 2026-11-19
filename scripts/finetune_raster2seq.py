"""Single-GPU Raster2Seq fine-tuning, gated on reviewed COCO annotations.

Uses the upstream model, tokenizer, dataset augmentation and losses without
the upstream NCCL/DDP launcher (which is Linux-specific). No cloud logging.
"""
import argparse
import hashlib
import json
import math
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate_dataset(root, expected=(24, 6)):
    """Require complete per-image review and no cross-partition building leaks."""
    groups, hashes, counts = {}, {}, {}
    for split, expected_count in zip(("train", "val"), expected):
        path = Path(root) / "annotations" / f"{split}.json"
        if not path.exists():
            raise ValueError(f"Missing {path}. Review and export polygon annotations first.")
        data = json.loads(path.read_text(encoding="utf-8"))
        counts[split] = len(data["images"])
        if counts[split] != expected_count:
            raise ValueError(f"Need {expected_count} reviewed {split} images, found {counts[split]}")
        annotated_ids = {a["image_id"] for a in data["annotations"]}
        for image in data["images"]:
            if not image.get("annotation_complete") or not image.get("group_verified"):
                raise ValueError("Every COCO image needs complete polygon and building-group review")
            if image["id"] not in annotated_ids:
                raise ValueError("A reviewed image has no instance annotations")
            if not (Path(root)/split/image["file_name"]).is_file():
                raise ValueError("A COCO image file is missing")
            for values, key in ((groups, image["group_id"]), (hashes, image["source_sha256"])):
                if key in values and values[key] != split:
                    raise ValueError("Building or duplicate source leaks across train/validation")
                values[key] = split
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT/"dataset/annotated_coco")
    parser.add_argument("--runtime", type=Path, default=ROOT/"training/raster2seq_runtime")
    parser.add_argument("--checkpoint", type=Path, default=ROOT/"models/pretrained/raster2seq/checkpoint.pth")
    parser.add_argument("--out", type=Path, default=ROOT/"models/checkpoints/raster2seq_pilot")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--lr", type=float, default=1e-5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--amp", action="store_true", help="Enable float16 autocast; default FP32 for reference-attention compatibility")
    parser.add_argument("--check-data", action="store_true")
    local = parser.parse_args()
    try:
        counts = validate_dataset(local.data)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    if local.check_data:
        print(json.dumps(counts)); return
    if local.epochs < 1 or local.lr <= 0:
        parser.error("epochs and learning rate must be positive")
    import numpy as np
    import torch
    from torch.utils.data import DataLoader
    from raster2seq_pipeline import CHECKPOINT_SHA256
    with local.checkpoint.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != CHECKPOINT_SHA256:
            parser.error("Base checkpoint does not match the verified upstream release")
    if not torch.cuda.is_available():
        parser.error("CUDA is required for this training runner")
    random.seed(local.seed); np.random.seed(local.seed); torch.manual_seed(local.seed)
    sys.path.insert(0, str(local.runtime))
    # Upstream engine parses its own evaluator options at import time.
    original_argv = sys.argv
    sys.argv = [sys.argv[0]]
    try:
        from predict import get_args_parser
        from datasets import build_dataset
        from models import build_model
        from engine import evaluate
    finally:
        sys.argv = original_argv
    args = get_args_parser().parse_args([])
    config = json.loads((ROOT/"models/pretrained/raster2seq/config.json").read_text())["inference_args"]
    vars(args).update(config)
    args.dataset_root = str(local.data)
    args.with_poly_refine = False
    args.wd_only = False
    args.converter_version = "v3"
    args.random_drop_rate = 0.0
    args.lr_backbone = local.lr/10
    args.cls_loss_coef, args.coords_loss_coef = 5, 20
    args.room_cls_loss_coef, args.raster_loss_coef = 0.2, 0
    args.label_smoothing = 0.1
    args.aux_loss = True
    training = build_dataset("train", args)
    validation = build_dataset("val", args)
    args.vocab_size = training.get_vocab_size()
    model, criterion = build_model(args, tokenizer=training.get_tokenizer())
    checkpoint = torch.load(local.checkpoint, map_location="cpu", weights_only=False)
    model.load_state_dict(checkpoint["model"], strict=True)
    del checkpoint
    model.cuda()
    def collate(batch):
        names = ("delta_x1","delta_x2","delta_y1","delta_y2","seq11","seq21","seq12","seq22",
                 "target_seq","token_labels","mask","target_polygon_labels","input_polygon_labels")
        extra = {name:torch.stack([item[name] for item in batch]) for name in names if name in batch[0]}
        return [{k:v for k,v in item.items() if k not in extra} for item in batch], extra
    train_loader = DataLoader(training, batch_size=1, shuffle=True, num_workers=0, collate_fn=collate)
    val_loader = DataLoader(validation, batch_size=1, shuffle=False, num_workers=0, collate_fn=collate)
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=local.lr, weight_decay=1e-4)
    scaler = torch.amp.GradScaler("cuda", enabled=local.amp)
    local.out.mkdir(parents=True, exist_ok=True)
    def measure():
        return evaluate(model, criterion, "cubicasa", val_loader, torch.device("cuda"),
                        poly2seq=True, per_token_sem_loss=True, output_dir=str(local.out))
    baseline = measure()
    (local.out/"baseline.json").write_text(json.dumps(baseline, default=float, indent=2))
    for epoch in range(1, local.epochs+1):
        model.train(); criterion.train(); total=0.0
        for batch, extra in train_loader:
            images=[item["image"].cuda() for item in batch]
            targets={key:value.cuda() for key,value in extra.items()}
            optimizer.zero_grad(set_to_none=True)
            with torch.autocast("cuda", dtype=torch.float16, enabled=local.amp):
                losses=criterion(model(images,targets),targets)
                loss=sum(value*criterion.weight_dict[key] for key,value in losses.items() if key in criterion.weight_dict)
            if not torch.isfinite(loss):
                raise RuntimeError("Non-finite loss; checkpoint not updated")
            scaler.scale(loss).backward(); scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(),0.1)
            scaler.step(optimizer); scaler.update()
            total+=float(loss.detach())
        metrics=measure()
        record={"epoch":epoch,"train_loss":total/len(train_loader),"validation":metrics}
        with (local.out/"metrics.jsonl").open("a",encoding="utf-8") as log:
            log.write(json.dumps(record,default=float)+"\n")
        pending=local.out/"last.pth.tmp"
        torch.save({"model":model.state_dict(),"optimizer":optimizer.state_dict(),"epoch":epoch,
                    "metrics":metrics,"args":vars(args),"seed":local.seed},pending)
        os.replace(pending,local.out/"last.pth")
        print(json.dumps(record,default=float),flush=True)


if __name__ == "__main__":
    main()
