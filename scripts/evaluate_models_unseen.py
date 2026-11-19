"""Evaluate Pretrained, Best, and Latest Raster2Seq models on unseen floor plans and genuine ground truth."""

import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import box, LineString, MultiPolygon, Point as ShapelyPoint, Polygon
from shapely.ops import unary_union

ROOT = Path("C:/Users/LENOVO/Desktop/multiCAD-mcp")
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_trace_engine import DOORS, EXT, INT, ROOMS, WALLS, WINDOWS
from trace_engine import compile_trace
from trace_engine.raster2seq_bridge import assess_trace_quality, clean_polygon, raster2seq_to_trace, unproject_point
from trace_fixtures import render_reference_plan

PYTHON_EXE = ROOT / ".venv-raster2seq" / "Scripts" / "python.exe"
RUNTIME_DIR = ROOT / "training" / "raster2seq_runtime"
PREDICT_PY = RUNTIME_DIR / "predict.py"

MODELS = {
    "pretrained": ROOT / "models" / "pretrained" / "raster2seq" / "checkpoint.pth",
    "checkpoint_best": ROOT / "models" / "checkpoints" / "r2seq_pilot30_finetune" / "checkpoint_best.pth",
    "checkpoint_latest": ROOT / "models" / "checkpoints" / "r2seq_pilot30_finetune" / "checkpoint_latest.pth",
}

EVAL_DIR = ROOT / "scratch" / "eval_unseen"
INPUTS_DIR = EVAL_DIR / "inputs"
OUTPUTS_DIR = EVAL_DIR / "outputs"
COMPARISONS_DIR = EVAL_DIR / "comparisons"

CC5K_CLASSES = {
    0: "Outdoor", 1: "Kitchen", 2: "Living Room", 3: "Bed Room", 4: "Bath",
    5: "Entry", 6: "Storage", 7: "Garage", 8: "Undefined", 9: "Window", 10: "Door"
}


def prepare_inputs() -> Dict[str, Path]:
    INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    COMPARISONS_DIR.mkdir(parents=True, exist_ok=True)

    inputs = {}

    # 1. Synthetic reference plan with mathematically exact ground truth
    ref_img, ref_info = render_reference_plan(mm_per_px=10.0, margin_px=120)
    ref_path = INPUTS_DIR / "reference_plan.png"
    ref_img.save(ref_path)
    inputs["reference_plan"] = ref_path

    # Save exact ground truth reference data
    gt_data = {
        "width_px": ref_img.width,
        "height_px": ref_img.height,
        "mm_per_px": ref_info["mm_per_px"],
        "origin_px": ref_info["origin_px"],
        "walls": {k: {"p1": v[0], "p2": v[1], "t": v[2]} for k, v in WALLS.items()},
        "rooms": {k: list(v) for k, v in ROOMS.items()},
        "doors": [{"wall": d[0], "offset": d[1], "width": d[2]} for d in DOORS],
        "windows": [{"wall": w[0], "offset": w[1], "width": w[2]} for w in WINDOWS],
    }
    with open(EVAL_DIR / "ground_truth_reference.json", "w", encoding="utf-8") as f:
        json.dump(gt_data, f, indent=2)

    # 2. Unseen real plan 1 (1-cgGpWW3i64vR0sLS8td9Xa1Fg8bOmos.jpg)
    real_1_src = ROOT / "dataset" / "cropped_plans" / "1-cgGpWW3i64vR0sLS8td9Xa1Fg8bOmos.jpg"
    real_1_dst = INPUTS_DIR / "unseen_plan_1.png"
    with Image.open(real_1_src) as im:
        im.convert("RGB").save(real_1_dst)
    inputs["unseen_plan_1"] = real_1_dst

    # 3. Unseen real plan 2 (1-E9BYYr8FiXAeFzIXACLtWgGbYVgc4vg.jpg)
    real_2_src = ROOT / "dataset" / "cropped_plans" / "1-E9BYYr8FiXAeFzIXACLtWgGbYVgc4vg.jpg"
    real_2_dst = INPUTS_DIR / "unseen_plan_2.png"
    with Image.open(real_2_src) as im:
        im.convert("RGB").save(real_2_dst)
    inputs["unseen_plan_2"] = real_2_dst

    return inputs


def run_inference(model_name: str, ckpt_path: Path):
    model_out = OUTPUTS_DIR / model_name
    model_out.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(PYTHON_EXE),
        "-B",
        "-u",
        str(PREDICT_PY),
        "--dataset_name", "cubicasa",
        "--dataset_root", str(INPUTS_DIR),
        "--checkpoint", str(ckpt_path),
        "--output_dir", str(model_out),
        "--semantic_classes", "12",
        "--input_channels", "3",
        "--poly2seq",
        "--seq_len", "512",
        "--num_bins", "32",
        "--disable_poly_refine",
        "--dec_attn_concat_src",
        "--per_token_sem_loss",
        "--use_anchor",
        "--ema4eval",
        "--save_pred",
        "--batch_size", "1",
        "--num_workers", "0",
    ]

    print(f"\n--- Running inference for {model_name} ---")
    log_file = model_out / "inference.log"
    with open(log_file, "w", encoding="utf-8") as lf:
        proc = subprocess.run(cmd, cwd=str(RUNTIME_DIR), stdout=lf, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        print(f"ERROR: Inference failed for {model_name}. See {log_file}")
        with open(log_file, "r", encoding="utf-8") as lf:
            print(lf.read()[-1000:])
        raise RuntimeError(f"Inference failed for {model_name}")
    print(f"Inference succeeded for {model_name}")


def load_model_predictions(model_name: str, plan_stem: str) -> Tuple[List[Dict], Path]:
    """Locate the json and floorplan visualization for a model and plan stem."""
    model_out = OUTPUTS_DIR / model_name
    # Search for json file
    json_candidates = list(model_out.glob(f"**/{plan_stem}.json"))
    if not json_candidates:
        raise FileNotFoundError(f"No prediction JSON found for {plan_stem} under {model_out}")
    with open(json_candidates[0], "r", encoding="utf-8") as f:
        preds = json.load(f)

    # Search for visualized floorplan image
    vis_candidates = list(model_out.glob(f"**/{plan_stem}_pred_floorplan.png"))
    vis_path = vis_candidates[0] if vis_candidates else None
    return preds, vis_path


def create_side_by_side(plan_name: str, input_path: Path):
    """Generate a clean side-by-side composite comparison image."""
    with Image.open(input_path) as orig_im:
        orig = orig_im.convert("RGB")

    target_h = 600
    w_scale = target_h / orig.height
    target_w = int(orig.width * w_scale)
    orig_resized = orig.resize((target_w, target_h), Image.Resampling.LANCZOS)

    panels = [("Original Input", orig_resized)]

    model_display_names = [
        ("pretrained", "Pretrained (CubiCasa5K Original)"),
        ("checkpoint_best", "Fine-Tuned (checkpoint_best.pth)"),
        ("checkpoint_latest", "Fine-Tuned (checkpoint_latest.pth)"),
    ]

    for model_key, label in model_display_names:
        _, vis_path = load_model_predictions(model_key, plan_name)
        if vis_path and vis_path.exists():
            with Image.open(vis_path) as v_im:
                v_res = v_im.convert("RGB").resize((target_h, target_h), Image.Resampling.LANCZOS)
        else:
            v_res = Image.new("RGB", (target_h, target_h), (240, 240, 240))
            d = ImageDraw.Draw(v_res)
            d.text((50, 250), "Visualization Missing", fill=(200, 50, 50))
        panels.append((label, v_res))

    # Composite all panels horizontally with headers
    header_h = 50
    total_w = sum(p[1].width for p in panels) + (len(panels) - 1) * 10
    total_h = target_h + header_h

    composite = Image.new("RGB", (total_w, total_h), (255, 255, 255))
    draw = ImageDraw.Draw(composite)

    try:
        font = ImageFont.truetype("arial.ttf", 20)
    except OSError:
        font = ImageFont.load_default()

    x_offset = 0
    for title, img in panels:
        # Draw header
        bbox = draw.textbbox((0, 0), title, font=font)
        text_w = bbox[2] - bbox[0]
        text_x = x_offset + (img.width - text_w) // 2
        draw.text((text_x, 15), title, fill=(20, 20, 30), font=font)

        # Paste panel
        composite.paste(img, (x_offset, header_h))
        # Draw border
        draw.rectangle([x_offset, header_h, x_offset + img.width, total_h - 1], outline=(200, 200, 200), width=1)
        x_offset += img.width + 10

    out_file = COMPARISONS_DIR / f"comparison_{plan_name}.png"
    composite.save(out_file)
    print(f"Saved side-by-side comparison to {out_file}")
    return out_file


def audit_connectivity_and_geometry(preds: List[Dict], img_w: int, img_h: int) -> Dict[str, Any]:
    """Audit wall connectivity, room boundaries, doors, and windows."""
    rooms = []
    openings = {"doors": [], "windows": []}

    for item in preds:
        cat_id = item.get("category_id")
        raw_pts = item.get("segmentation", [])
        if not raw_pts:
            continue

        if cat_id == 10:  # Door
            if len(raw_pts) >= 2:
                p1 = unproject_point(raw_pts[0][0], raw_pts[0][1], img_w, img_h)
                p2 = unproject_point(raw_pts[-1][0], raw_pts[-1][1], img_w, img_h)
                openings["doors"].append((p1, p2))
        elif cat_id == 9:  # Window
            if len(raw_pts) >= 2:
                p1 = unproject_point(raw_pts[0][0], raw_pts[0][1], img_w, img_h)
                p2 = unproject_point(raw_pts[-1][0], raw_pts[-1][1], img_w, img_h)
                openings["windows"].append((p1, p2))
        elif cat_id in CC5K_CLASSES and cat_id < 9:
            if len(raw_pts) >= 3:
                unproj = [unproject_point(p[0], p[1], img_w, img_h) for p in raw_pts]
                poly = clean_polygon(unproj)
                if poly is not None and poly.area > 50:
                    rooms.append({"cat_id": cat_id, "name": CC5K_CLASSES[cat_id], "poly": poly})

    # Compile trace with raster2seq_to_trace
    trace_res = raster2seq_to_trace(preds, (img_w, img_h), mm_per_px=10.0)
    topology_quality = assess_trace_quality(trace_res)

    # Graph analysis
    nodes = trace_res.walls_graph.nodes
    edges = trace_res.walls_graph.edges

    degree_map = {n.id: 0 for n in nodes}
    for e in edges:
        degree_map[e.source] = degree_map.get(e.source, 0) + 1
        degree_map[e.target] = degree_map.get(e.target, 0) + 1

    dead_ends = sum(1 for d in degree_map.values() if d == 1)
    corners_and_joints = sum(1 for d in degree_map.values() if d >= 2)

    # Count connected components in wall graph
    adj = {n.id: [] for n in nodes}
    for e in edges:
        adj[e.source].append(e.target)
        adj[e.target].append(e.source)

    visited = set()
    num_components = 0
    for nid in adj:
        if nid not in visited:
            num_components += 1
            queue = [nid]
            visited.add(nid)
            while queue:
                curr = queue.pop(0)
                for neighbor in adj[curr]:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)

    id2node = {n.id: n for n in nodes}
    total_wall_length_m = sum(
        math.hypot(id2node[e.target].x - id2node[e.source].x, id2node[e.target].y - id2node[e.source].y)
        for e in edges
    ) / 1000.0

    return {
        "room_count": len(rooms),
        "valid_closed_rooms": sum(1 for r in rooms if r["poly"].is_valid and r["poly"].area > 0),
        "room_categories": {CC5K_CLASSES[i]: sum(1 for r in rooms if r["cat_id"] == i) for i in range(9)},
        "door_count": len(openings["doors"]),
        "window_count": len(openings["windows"]),
        "wall_edges_count": len(edges),
        "wall_nodes_count": len(nodes),
        "dead_ends": dead_ends,
        "corners_and_joints": corners_and_joints,
        "connected_components": num_components,
        "total_wall_length_m": round(total_wall_length_m, 2),
        "topology_quality": topology_quality.as_dict(),
    }


def evaluate_against_ground_truth(preds: List[Dict], ref_w: int, ref_h: int) -> Dict[str, Any]:
    """Calculate accuracy against genuine ground-truth annotations on reference plan."""
    # Build Ground Truth polygons and lines in pixel coordinates
    mm_per_px = 10.0
    margin_px = 120
    height_mm = 12192

    def px(p):
        return (margin_px + p[0] / mm_per_px, margin_px + (height_mm - p[1]) / mm_per_px)

    gt_room_polys = {}
    for rname, (xl, xr, yb, yt) in ROOMS.items():
        p1 = px((xl, yt))
        p2 = px((xr, yt))
        p3 = px((xr, yb))
        p4 = px((xl, yb))
        poly = Polygon([p1, p2, p3, p4])
        if poly.is_valid:
            gt_room_polys[rname] = poly

    gt_total_room_area = unary_union(list(gt_room_polys.values())).area

    # Extract predicted room polygons
    pred_polys = []
    for item in preds:
        cat_id = item.get("category_id")
        raw_pts = item.get("segmentation", [])
        if cat_id is not None and cat_id < 9 and len(raw_pts) >= 3:
            unproj = [unproject_point(p[0], p[1], ref_w, ref_h) for p in raw_pts]
            p = clean_polygon(unproj)
            if p is not None and p.area > 50:
                pred_polys.append(p)

    pred_union = unary_union(pred_polys) if pred_polys else Polygon()
    gt_union = unary_union(list(gt_room_polys.values()))

    # Calculate Room IoU
    intersection_area = gt_union.intersection(pred_union).area if pred_union.area > 0 else 0.0
    union_area = gt_union.union(pred_union).area if pred_union.area > 0 else gt_union.area
    room_iou = intersection_area / union_area if union_area > 0 else 0.0

    # Door and Window metrics
    pred_doors = []
    pred_windows = []
    for item in preds:
        cat_id = item.get("category_id")
        raw_pts = item.get("segmentation", [])
        if len(raw_pts) >= 2:
            p1 = unproject_point(raw_pts[0][0], raw_pts[0][1], ref_w, ref_h)
            p2 = unproject_point(raw_pts[-1][0], raw_pts[-1][1], ref_w, ref_h)
            mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
            if cat_id == 10:
                pred_doors.append(mid)
            elif cat_id == 9:
                pred_windows.append(mid)

    # Compute Ground Truth Door & Window Centers in pixels
    def gt_opening_center_px(wall_name, offset, width):
        a, b, _t = WALLS[wall_name]
        length = math.dist(a, b)
        s = offset + width / 2.0
        pos_mm = (a[0] + (b[0] - a[0]) * s / length, a[1] + (b[1] - a[1]) * s / length)
        return px(pos_mm)

    gt_door_centers = [gt_opening_center_px(w, o, wd) for w, o, wd in DOORS]
    gt_win_centers = [gt_opening_center_px(w, o, wd) for w, o, wd in WINDOWS]

    # Matching doors within 80 pixels tolerance (~800mm)
    tol_px = 80.0
    matched_doors = 0
    door_errors = []
    for gtd in gt_door_centers:
        closest = min([math.dist(gtd, pd) for pd in pred_doors], default=9999.0)
        if closest <= tol_px:
            matched_doors += 1
            door_errors.append(closest * mm_per_px)

    matched_windows = 0
    win_errors = []
    for gtw in gt_win_centers:
        closest = min([math.dist(gtw, pw) for pw in pred_windows], default=9999.0)
        if closest <= tol_px:
            matched_windows += 1
            win_errors.append(closest * mm_per_px)

    # Wall coverage: evaluate predicted walls against GT wall centerlines
    gt_wall_lines = []
    for (a, b, t) in WALLS.values():
        gt_wall_lines.append(LineString([px(a), px(b)]))
    gt_wall_union = unary_union(gt_wall_lines)

    trace = raster2seq_to_trace(preds, (ref_w, ref_h), mm_per_px=10.0)
    pred_wall_lines = []
    id2node = {n.id: n for n in trace.walls_graph.nodes}
    for e in trace.walls_graph.edges:
        u, v = id2node[e.source], id2node[e.target]
        p_u = (u.x / mm_per_px, (height_mm - u.y) / mm_per_px)
        p_v = (v.x / mm_per_px, (height_mm - v.y) / mm_per_px)
        pred_wall_lines.append(LineString([p_u, p_v]))

    wall_buffer_gt = gt_wall_union.buffer(20.0)  # 200 mm buffer
    covered_len = 0.0
    total_pred_len = 0.0
    for pw in pred_wall_lines:
        total_pred_len += pw.length
        intersect = pw.intersection(wall_buffer_gt)
        if not intersect.is_empty:
            covered_len += intersect.length
    wall_precision = (covered_len / total_pred_len) if total_pred_len > 0 else 0.0

    pred_wall_buffer = unary_union(pred_wall_lines).buffer(20.0) if pred_wall_lines else Polygon()
    gt_covered_len = 0.0
    total_gt_len = 0.0
    for gw in gt_wall_lines:
        total_gt_len += gw.length
        if not pred_wall_buffer.is_empty:
            inter = gw.intersection(pred_wall_buffer)
            if not inter.is_empty:
                gt_covered_len += inter.length
    wall_recall = (gt_covered_len / total_gt_len) if total_gt_len > 0 else 0.0

    return {
        "room_iou_pct": round(room_iou * 100.0, 2),
        "gt_room_count": len(ROOMS),
        "pred_room_count": len(pred_polys),
        "gt_door_count": len(DOORS),
        "pred_door_count": len(pred_doors),
        "matched_doors": matched_doors,
        "door_recall_pct": round(matched_doors / len(DOORS) * 100.0, 1),
        "door_mean_offset_mm": round(np.mean(door_errors), 1) if door_errors else None,
        "gt_window_count": len(WINDOWS),
        "pred_window_count": len(pred_windows),
        "matched_windows": matched_windows,
        "window_recall_pct": round(matched_windows / len(WINDOWS) * 100.0, 1),
        "window_mean_offset_mm": round(np.mean(win_errors), 1) if win_errors else None,
        "wall_precision_pct": round(wall_precision * 100.0, 2),
        "wall_recall_pct": round(wall_recall * 100.0, 2),
    }


def main():
    print("Preparing unseen test inputs and ground-truth benchmark...")
    inputs = prepare_inputs()

    for m_name, m_path in MODELS.items():
        if not m_path.exists():
            raise FileNotFoundError(f"Checkpoint not found: {m_path}")
        has_preds = all(
            len(list((OUTPUTS_DIR / m_name).glob(f"**/{pname}.json"))) > 0
            for pname in inputs
        )
        if has_preds and "--force-inference" not in sys.argv:
            print(f"Predictions already exist for {m_name}, skipping inference.")
        else:
            run_inference(m_name, m_path)

    print("\nGenerating side-by-side composite images...")
    comparisons = {}
    for plan_name, in_path in inputs.items():
        comp_path = create_side_by_side(plan_name, in_path)
        comparisons[plan_name] = str(comp_path)

    print("\nRunning Architectural Audits across all models...")
    audit_results = {}
    gt_eval_results = {}

    for plan_name, in_path in inputs.items():
        audit_results[plan_name] = {}
        with Image.open(in_path) as im:
            w, h = im.size

        for m_name in MODELS:
            preds, _ = load_model_predictions(m_name, plan_name)
            audit_results[plan_name][m_name] = audit_connectivity_and_geometry(preds, w, h)

            if plan_name == "reference_plan":
                gt_eval_results[m_name] = evaluate_against_ground_truth(preds, w, h)

    full_report = {
        "comparisons": comparisons,
        "architectural_audits": audit_results,
        "ground_truth_accuracy_reference_plan": gt_eval_results,
    }

    report_path = EVAL_DIR / "model_evaluation_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(full_report, f, indent=2)

    print(f"\nAll evaluations complete! Full report written to {report_path}")


if __name__ == "__main__":
    main()
