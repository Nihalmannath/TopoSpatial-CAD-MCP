"""Generate architectural overlays of walls, rooms, doors, and windows on original plans."""

import json
import math
from pathlib import Path
from typing import Dict, List, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path("C:/Users/LENOVO/Desktop/multiCAD-mcp")
EVAL_DIR = ROOT / "scratch" / "eval_unseen"
INPUTS_DIR = EVAL_DIR / "inputs"
OUTPUTS_DIR = EVAL_DIR / "outputs"
OVERLAYS_DIR = EVAL_DIR / "visual_overlays"
ARTIFACT_DIR = Path("C:/Users/LENOVO/.gemini/antigravity/brain/9ecc3f33-cac7-4919-af36-ed7d0bcea60a")

sys_path = ROOT / "src"
import sys
if str(sys_path) not in sys.path:
    sys.path.insert(0, str(sys_path))

from trace_engine.raster2seq_bridge import clean_polygon, unproject_point

MODELS = [
    ("pretrained", "Pretrained (CubiCasa5K)", OUTPUTS_DIR / "pretrained" / "raster2seq" / "jsons"),
    ("checkpoint_best", "Fine-Tuned (checkpoint_best.pth)", OUTPUTS_DIR / "checkpoint_best" / "r2seq_pilot30_finetune" / "jsons"),
    ("checkpoint_latest", "Fine-Tuned (checkpoint_latest.pth)", OUTPUTS_DIR / "checkpoint_latest" / "r2seq_pilot30_finetune" / "jsons"),
]

PLANS = [
    ("reference_plan", "Synthetic Ground-Truth Reference Plan", INPUTS_DIR / "reference_plan.png"),
    ("unseen_plan_1", "Unseen Plan 1 (Line Style)", INPUTS_DIR / "unseen_plan_1.png"),
    ("unseen_plan_2", "Unseen Plan 2 (Dense Architectural)", INPUTS_DIR / "unseen_plan_2.png"),
]

# Color palette: BGR for OpenCV
ROOM_COLOR_BGR = (255, 180, 100)    # Soft blue/cyan
WALL_COLOR_BGR = (40, 40, 220)      # Solid crimson/red
DOOR_COLOR_BGR = (60, 200, 60)      # Green
WINDOW_COLOR_BGR = (220, 200, 40)   # Teal/cyan


def render_overlay(input_img_path: Path, preds: List[Dict]) -> Image.Image:
    with Image.open(input_img_path) as im:
        orig = im.convert("RGB")
    orig_np = np.array(orig)
    h, w = orig_np.shape[:2]

    # Overlay layer for transparency
    overlay = orig_np.copy()

    rooms = []
    doors = []
    windows = []

    for item in preds:
        cid = item.get("category_id")
        raw_pts = item.get("segmentation", [])
        if not raw_pts:
            continue

        if cid == 10:  # Door
            if len(raw_pts) >= 2:
                p1 = unproject_point(raw_pts[0][0], raw_pts[0][1], w, h)
                p2 = unproject_point(raw_pts[-1][0], raw_pts[-1][1], w, h)
                doors.append((tuple(map(int, p1)), tuple(map(int, p2))))
        elif cid == 9:  # Window
            if len(raw_pts) >= 2:
                p1 = unproject_point(raw_pts[0][0], raw_pts[0][1], w, h)
                p2 = unproject_point(raw_pts[-1][0], raw_pts[-1][1], w, h)
                windows.append((tuple(map(int, p1)), tuple(map(int, p2))))
        elif cid is not None and cid < 9:  # Room
            if len(raw_pts) >= 3:
                unproj = [unproject_point(p[0], p[1], w, h) for p in raw_pts]
                poly = clean_polygon(unproj)
                if poly is not None and poly.area > 50:
                    coords = np.array(poly.exterior.coords, dtype=np.int32)
                    rooms.append(coords)

    # 1. Draw transparent room fills
    room_layer = overlay.copy()
    for r in rooms:
        cv2.fillPoly(room_layer, [r], (220, 235, 255))  # Soft blue
    cv2.addWeighted(room_layer, 0.45, overlay, 0.55, 0, overlay)

    # 2. Draw room outlines (walls)
    for r in rooms:
        cv2.polylines(overlay, [r], isClosed=True, color=(180, 50, 40), thickness=3)

    # 3. Draw doors (bold green)
    for p1, p2 in doors:
        cv2.line(overlay, p1, p2, (30, 180, 30), thickness=5)
        # Draw small circle at endpoints
        cv2.circle(overlay, p1, 4, (20, 140, 20), -1)
        cv2.circle(overlay, p2, 4, (20, 140, 20), -1)

    # 4. Draw windows (bold cyan/teal)
    for p1, p2 in windows:
        cv2.line(overlay, p1, p2, (200, 160, 20), thickness=4)
        cv2.circle(overlay, p1, 4, (160, 120, 10), -1)
        cv2.circle(overlay, p2, 4, (160, 120, 10), -1)

    # Blend overlay with original
    result = Image.fromarray(overlay)
    return result


def main():
    OVERLAYS_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    print("Generating overlays...")
    for plan_key, plan_label, img_path in PLANS:
        print(f"\nProcessing plan: {plan_key} ({plan_label})")
        with Image.open(img_path) as im:
            orig = im.convert("RGB")

        target_h = 600
        w_scale = target_h / orig.height
        target_w = int(orig.width * w_scale)

        panels = [("Original Drawing", orig.resize((target_w, target_h), Image.Resampling.LANCZOS))]

        for model_key, model_label, jsons_dir in MODELS:
            j_path = jsons_dir / f"{plan_key}.json"
            if not j_path.exists():
                print(f"  Missing {j_path}")
                continue
            with open(j_path, "r", encoding="utf-8") as f:
                preds = json.load(f)

            overlay_img = render_overlay(img_path, preds)
            overlay_resized = overlay_img.resize((target_w, target_h), Image.Resampling.LANCZOS)
            panels.append((model_label, overlay_resized))

        # Composite montage
        header_h = 50
        total_w = sum(p[1].width for p in panels) + (len(panels) - 1) * 8
        total_h = target_h + header_h

        composite = Image.new("RGB", (total_w, total_h), (255, 255, 255))
        draw = ImageDraw.Draw(composite)

        try:
            font = ImageFont.truetype("arial.ttf", 18)
        except OSError:
            font = ImageFont.load_default()

        x_offset = 0
        for title, img in panels:
            bbox = draw.textbbox((0, 0), title, font=font)
            text_w = bbox[2] - bbox[0]
            text_x = x_offset + (img.width - text_w) // 2
            draw.text((text_x, 15), title, fill=(20, 20, 30), font=font)

            composite.paste(img, (x_offset, header_h))
            draw.rectangle([x_offset, header_h, x_offset + img.width, total_h - 1], outline=(210, 210, 210), width=1)
            x_offset += img.width + 8

        out_local = OVERLAYS_DIR / f"overlay_montage_{plan_key}.png"
        out_artifact = ARTIFACT_DIR / f"overlay_montage_{plan_key}.png"
        composite.save(out_local)
        composite.save(out_artifact)
        print(f"  Saved overlay montage to: {out_local} and {out_artifact}")

    print("\nAll overlay montages generated successfully!")


if __name__ == "__main__":
    main()
