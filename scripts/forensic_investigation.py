"""Forensic analysis script for Raster2Seq fine-tuning failure."""

import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import torch
from shapely.geometry import Polygon

ROOT = Path("C:/Users/LENOVO/Desktop/multiCAD-mcp")
EVAL_DIR = ROOT / "scratch" / "eval_unseen"
COCO_DIR = ROOT / "dataset" / "annotated_coco" / "annotations"
PILOT_JSON = ROOT / "dataset" / "pilot_30.json"
CHECKPOINTS_DIR = ROOT / "models" / "checkpoints" / "r2seq_pilot30_finetune"


def inspect_predictions():
    print("==================================================================")
    print("1. PREDICTION FORENSICS (Pretrained vs Best vs Latest)")
    print("==================================================================")

    models = {
        "pretrained": EVAL_DIR / "outputs" / "pretrained" / "raster2seq" / "jsons",
        "checkpoint_best": EVAL_DIR / "outputs" / "checkpoint_best" / "r2seq_pilot30_finetune" / "jsons",
        "checkpoint_latest": EVAL_DIR / "outputs" / "checkpoint_latest" / "r2seq_pilot30_finetune" / "jsons",
    }

    test_plans = ["reference_plan", "unseen_plan_1", "unseen_plan_2"]

    for m_name, j_dir in models.items():
        print(f"\n--- Model: {m_name} ---")
        for plan in test_plans:
            j_file = j_dir / f"{plan}.json"
            if not j_file.exists():
                print(f"  {plan}: FILE NOT FOUND ({j_file})")
                continue
            with open(j_file, "r", encoding="utf-8") as f:
                preds = json.load(f)

            num_instances = len(preds)
            cat_counts = {}
            vert_counts = []
            for item in preds:
                cid = item.get("category_id")
                cat_counts[cid] = cat_counts.get(cid, 0) + 1
                seg = item.get("segmentation", [])
                vert_counts.append(len(seg))

            print(f"  Plan: {plan}")
            print(f"    Total predicted instances: {num_instances}")
            print(f"    Category counts: {cat_counts}")
            print(f"    Vertices per polygon: min={min(vert_counts) if vert_counts else 0}, "
                  f"max={max(vert_counts) if vert_counts else 0}, "
                  f"mean={np.mean(vert_counts) if vert_counts else 0:.1f}")


def inspect_training_history():
    print("\n==================================================================")
    print("2. TRAINING DYNAMICS & CHECKPOINT SELECTION")
    print("==================================================================")

    hist_file = CHECKPOINTS_DIR / "training_history.json"
    if not hist_file.exists():
        print("training_history.json not found!")
        return

    with open(hist_file, "r") as f:
        history = json.load(f)

    print(f"Total recorded epochs: {len(history)}")
    for row in history:
        print(f"  Epoch {row['epoch']:2d}: Loss = {row['loss']:.4f}, LR = {row['lr']:.2e}, Time = {row['time_s']:.1f}s")

    losses = [r["loss"] for r in history]
    min_epoch = int(np.argmin(losses)) + 1
    print(f"\nMinimum loss occurred at: Epoch {min_epoch} (Loss: {min(losses):.4f})")
    print(f"Final epoch loss: Epoch {history[-1]['epoch']} (Loss: {history[-1]['loss']:.4f})")
    print(f"Loss ratio (Final / Min): {history[-1]['loss'] / min(losses):.2f}x")


def inspect_coco_dataset():
    print("\n==================================================================")
    print("3. COCO DATASET & PSEUDO-LABEL PROVENANCE")
    print("==================================================================")

    # Check pilot_30.json
    with open(PILOT_JSON, "r") as f:
        pilot = json.load(f)
    print(f"Pilot metadata status: {pilot.get('status')}")
    completed_flags = [p.get("annotation_complete", False) for p in pilot["plans"]]
    verified_flags = [p.get("group_verified", False) for p in pilot["plans"]]
    print(f"Plans with annotation_complete=True: {sum(completed_flags)} / {len(completed_flags)}")
    print(f"Plans with group_verified=True: {sum(verified_flags)} / {len(verified_flags)}")

    # Check COCO files
    for split in ["train", "val"]:
        c_file = COCO_DIR / f"{split}.json"
        if not c_file.exists():
            print(f"COCO file not found: {c_file}")
            continue
        with open(c_file, "r", encoding="utf-8") as f:
            coco = json.load(f)

        print(f"\nCOCO {split}.json:")
        print(f"  Images: {len(coco['images'])}")
        print(f"  Annotations: {len(coco['annotations'])}")

        # Check door/window annotations: 2-point vs 4-point
        door_vert_lens = []
        win_vert_lens = []
        room_vert_lens = []
        invalid_geoms = 0
        zero_area = 0

        for ann in coco["annotations"]:
            cid = ann["category_id"]
            seg = ann["segmentation"][0]
            pts = list(zip(seg[0::2], seg[1::2]))
            if cid == 10:
                door_vert_lens.append(len(pts))
            elif cid == 9:
                win_vert_lens.append(len(pts))
            else:
                room_vert_lens.append(len(pts))

            if len(pts) < 3:
                invalid_geoms += 1
            else:
                p = Polygon(pts)
                if not p.is_valid:
                    invalid_geoms += 1
                if p.area <= 0:
                    zero_area += 1

        print(f"  Room instances: {len(room_vert_lens)}, mean vertices: {np.mean(room_vert_lens):.1f}")
        print(f"  Door instances: {len(door_vert_lens)}, vertices: {set(door_vert_lens)}")
        print(f"  Window instances: {len(win_vert_lens)}, vertices: {set(win_vert_lens)}")
        print(f"  Invalid geometries: {invalid_geoms}, Zero area: {zero_area}")


def inspect_model_weights():
    print("\n==================================================================")
    print("4. MODEL WEIGHTS & CONFIGURATION CHECK")
    print("==================================================================")

    ck_orig = ROOT / "models" / "pretrained" / "raster2seq" / "checkpoint.pth"
    ck_best = CHECKPOINTS_DIR / "checkpoint_best.pth"
    ck_latest = CHECKPOINTS_DIR / "checkpoint_latest.pth"

    data_orig = torch.load(ck_orig, map_location="cpu", weights_only=False)
    data_best = torch.load(ck_best, map_location="cpu", weights_only=False)
    data_latest = torch.load(ck_latest, map_location="cpu", weights_only=False)

    print(f"Original checkpoint size: {ck_orig.stat().st_size / 1e6:.1f} MB, keys: {list(data_orig.keys())}")
    print(f"Best checkpoint size:     {ck_best.stat().st_size / 1e6:.1f} MB, keys: {list(data_best.keys())}")
    print(f"Latest checkpoint size:   {ck_latest.stat().st_size / 1e6:.1f} MB, keys: {list(data_latest.keys())}")

    print(f"\nBest Checkpoint Epoch:   {data_best.get('epoch')}")
    print(f"Latest Checkpoint Epoch: {data_latest.get('epoch')}")

    # Compare weights between original and best
    orig_sd = data_orig["model"]
    best_sd = data_best["model"]
    latest_sd = data_latest["model"]

    # Sample weight difference (e.g. transformer decoder query embed or layer 0 weights)
    diff_orig_best = []
    diff_orig_latest = []
    for k in best_sd:
        clean_k = k[7:] if k.startswith("module.") else k
        if clean_k in orig_sd:
            w_orig = orig_sd[clean_k].float()
            w_best = best_sd[k].float()
            w_lat = latest_sd[k].float()
            diff_orig_best.append(torch.mean(torch.abs(w_best - w_orig)).item())
            diff_orig_latest.append(torch.mean(torch.abs(w_lat - w_orig)).item())

    print(f"\nMean Absolute Weight Difference (Best vs Orig):   {np.mean(diff_orig_best):.6f}")
    print(f"Mean Absolute Weight Difference (Latest vs Orig): {np.mean(diff_orig_latest):.6f}")


if __name__ == "__main__":
    inspect_predictions()
    inspect_training_history()
    inspect_coco_dataset()
    inspect_model_weights()
