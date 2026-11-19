"""scripts/style_survey.py

Survey all dataset images, classify by visual style (line / grey-filled / render / solid),
run both classic and neural engines with apply=False, generate overlays, and produce
survey.csv and survey.html.
"""

from __future__ import annotations

import argparse
import csv
import html
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
from PIL import Image

# Ensure src is on sys.path
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from trace_engine import compile_trace  # noqa: E402
from trace_engine.vision import render_overlay, trace_image  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("style_survey")


def classify_style(image_path: str) -> Tuple[str, Dict[str, float]]:
    """Classify image style via colour and darkness statistics."""
    im = Image.open(image_path)
    rgb = np.asarray(im)
    if rgb.ndim == 2:
        col_diff = 0.0
        pct_col = 0.0
        gray = rgb
    else:
        col_diff = float(np.mean(np.max(rgb, axis=2) - np.min(rgb, axis=2)))
        pct_col = float(np.mean(np.max(rgb, axis=2) - np.min(rgb, axis=2) > 20) * 100)
        gray = np.asarray(im.convert("L"))

    pct_dark = float(np.mean(gray < 50) * 100)
    pct_grey = float(np.mean((gray >= 50) & (gray < 210)) * 100)

    # 3D colour renders have widespread colour (flooring, furniture, grass)
    if pct_col > 15.0 or (col_diff > 10.0 and pct_col > 8.0):
        style = "render"
    elif pct_grey > 7.0:
        style = "grey-filled"
    elif pct_dark > 4.5:
        style = "solid"
    else:
        style = "line"

    stats = {
        "pct_col": round(pct_col, 2),
        "col_diff": round(col_diff, 2),
        "pct_dark": round(pct_dark, 2),
        "pct_grey": round(pct_grey, 2),
    }
    return style, stats


def check_usable(rooms: int, walls: int, footprint_mm: Sequence[float]) -> bool:
    """A usable draft has >=5 rooms, a plausible footprint (5-30m per side),
    and is not flooded (walls <= 3 x rooms + 20)."""
    if rooms < 5:
        return False
    if len(footprint_mm) < 2:
        return False
    w_m = footprint_mm[0] / 1000.0
    d_m = footprint_mm[1] / 1000.0
    if not (5.0 <= w_m <= 30.0 and 5.0 <= d_m <= 30.0):
        return False
    if walls > (3 * rooms + 20):
        return False
    return True


def run_engine_trace(img_path: str, engine: str, out_overlay_path: Path) -> Dict[str, Any]:
    """Run single engine trace with apply=False and generate overlay."""
    t0 = time.time()
    try:
        tr, rep = trace_image(img_path, engine=engine, ocr=True)
        comp = compile_trace(tr)
        summary = comp.summary()
        counts = summary.get("counts", {})
        rooms_list = summary.get("rooms", [])
        footprint = summary.get("footprint_mm", [0, 0])
        walls = counts.get("wall", 0)
        doors = counts.get("door", 0)
        windows = counts.get("window", 0)
        openings = doors + windows
        rooms = len(rooms_list)
        named_rooms = sum(1 for r in rooms_list if not r["name"].startswith("Space "))
        unnamed_rooms = rooms - named_rooms
        scale_from = rep.scale_method
        scale_mm_per_px = rep.mm_per_px
        usable = check_usable(rooms, walls, footprint)

        overlay_rel = ""
        try:
            render_overlay(img_path, tr, comp, str(out_overlay_path))
            overlay_rel = f"overlays/{out_overlay_path.name}"
        except Exception as ov_err:
            logger.warning("Failed to render overlay for %s (%s): %s", img_path, engine, ov_err)

        return {
            "status": "ok",
            "error": "",
            "seconds": round(time.time() - t0, 2),
            "rooms": rooms,
            "walls": walls,
            "openings": openings,
            "doors": doors,
            "windows": windows,
            "named_rooms": named_rooms,
            "unnamed_rooms": unnamed_rooms,
            "scale_from": scale_from,
            "scale_mm_per_px": round(scale_mm_per_px, 3),
            "footprint_w_m": round(footprint[0] / 1000.0, 2),
            "footprint_d_m": round(footprint[1] / 1000.0, 2),
            "usable": usable,
            "overlay": overlay_rel,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "seconds": round(time.time() - t0, 2),
            "rooms": 0,
            "walls": 0,
            "openings": 0,
            "doors": 0,
            "windows": 0,
            "named_rooms": 0,
            "unnamed_rooms": 0,
            "scale_from": "",
            "scale_mm_per_px": 0.0,
            "footprint_w_m": 0.0,
            "footprint_d_m": 0.0,
            "usable": False,
            "overlay": "",
        }


CSV_FIELDNAMES = [
    "stem",
    "filename",
    "style",
    "pct_col",
    "col_diff",
    "pct_dark",
    "pct_grey",
    "classic_status",
    "classic_error",
    "classic_seconds",
    "classic_rooms",
    "classic_walls",
    "classic_openings",
    "classic_doors",
    "classic_windows",
    "classic_named_rooms",
    "classic_unnamed_rooms",
    "classic_scale_from",
    "classic_scale_mm_per_px",
    "classic_footprint_w_m",
    "classic_footprint_d_m",
    "classic_usable",
    "classic_overlay",
    "neural_status",
    "neural_error",
    "neural_seconds",
    "neural_rooms",
    "neural_walls",
    "neural_openings",
    "neural_doors",
    "neural_windows",
    "neural_named_rooms",
    "neural_unnamed_rooms",
    "neural_scale_from",
    "neural_scale_mm_per_px",
    "neural_footprint_w_m",
    "neural_footprint_d_m",
    "neural_usable",
    "neural_overlay",
]


def render_html_report(rows: List[Dict[str, Any]], out_path: Path) -> None:
    """Write an interactive, grouped contact sheet survey.html."""
    styles = ["line", "grey-filled", "render", "solid"]
    style_labels = {
        "line": "Line Drawings (~50%)",
        "grey-filled": "Grey-Filled Walls (~25%)",
        "render": "3D Colour Renders (~25%)",
        "solid": "Solid Black Walls (<5%)",
    }

    # Summary table stats
    summary_stats: Dict[str, Dict[str, Any]] = {}
    for s in styles:
        s_rows = [r for r in rows if r["style"] == s]
        total = len(s_rows)
        c_use = sum(1 for r in s_rows if str(r.get("classic_usable")).lower() == "true")
        n_use = sum(1 for r in s_rows if str(r.get("neural_usable")).lower() == "true")
        either_use = sum(
            1 for r in s_rows
            if str(r.get("classic_usable")).lower() == "true" or str(r.get("neural_usable")).lower() == "true"
        )
        c_pct = (c_use / total * 100) if total else 0
        n_pct = (n_use / total * 100) if total else 0
        e_pct = (either_use / total * 100) if total else 0
        summary_stats[s] = {
            "total": total,
            "classic_usable": c_use,
            "classic_pct": c_pct,
            "neural_usable": n_use,
            "neural_pct": n_pct,
            "either_usable": either_use,
            "either_pct": e_pct,
        }

    tot_all = len(rows)
    tot_c = sum(1 for r in rows if str(r.get("classic_usable")).lower() == "true")
    tot_n = sum(1 for r in rows if str(r.get("neural_usable")).lower() == "true")
    tot_e = sum(
        1 for r in rows
        if str(r.get("classic_usable")).lower() == "true" or str(r.get("neural_usable")).lower() == "true"
    )

    summary_stats["TOTAL"] = {
        "total": tot_all,
        "classic_usable": tot_c,
        "classic_pct": (tot_c / tot_all * 100) if tot_all else 0,
        "neural_usable": tot_n,
        "neural_pct": (tot_n / tot_all * 100) if tot_all else 0,
        "either_usable": tot_e,
        "either_pct": (tot_e / tot_all * 100) if tot_all else 0,
    }

    html_parts = [
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dataset Reality Check & Style Survey</title>
<style>
  :root {
    --bg: #0f172a;
    --card-bg: #1e293b;
    --card-border: #334155;
    --text: #f8fafc;
    --text-muted: #94a3b8;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #f59e0b;
    --blue: #3b82f6;
  }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    margin: 0;
    padding: 24px;
    line-height: 1.5;
  }
  h1, h2, h3 { margin-top: 0; font-weight: 600; }
  h1 { font-size: 1.8rem; margin-bottom: 8px; color: #fff; }
  .subtitle { color: var(--text-muted); margin-bottom: 24px; font-size: 0.95rem; }
  
  .summary-table {
    width: 100%;
    border-collapse: collapse;
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 32px;
  }
  .summary-table th, .summary-table td {
    padding: 12px 16px;
    text-align: left;
    border-bottom: 1px solid var(--card-border);
  }
  .summary-table th { background: #111e33; color: var(--text-muted); font-size: 0.85rem; text-transform: uppercase; }
  .summary-table tr:last-child td { font-weight: bold; background: #182338; }
  
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .badge-usable { background: rgba(34, 197, 94, 0.2); color: var(--green); border: 1px solid var(--green); }
  .badge-bad { background: rgba(239, 68, 68, 0.2); color: var(--red); border: 1px solid var(--red); }
  .badge-style { background: rgba(59, 130, 246, 0.2); color: var(--blue); border: 1px solid var(--blue); }

  .filter-bar {
    display: flex;
    gap: 8px;
    margin-bottom: 24px;
    flex-wrap: wrap;
  }
  .filter-btn {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    color: var(--text);
    padding: 8px 16px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.85rem;
    transition: all 0.2s;
  }
  .filter-btn:hover, .filter-btn.active {
    background: var(--blue);
    border-color: var(--blue);
  }

  .style-section { margin-bottom: 40px; }
  .style-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 2px solid var(--card-border);
    padding-bottom: 8px;
    margin-bottom: 16px;
  }
  .card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(460px, 1fr));
    gap: 20px;
  }
  .card {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 8px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .card-title {
    font-size: 0.95rem;
    font-family: monospace;
    font-weight: bold;
    display: flex;
    justify-content: space-between;
    align-items: center;
    word-break: break-all;
  }
  .views-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }
  .view-col {
    background: #090d16;
    border-radius: 6px;
    padding: 6px;
    text-align: center;
    border: 1px solid #1e293b;
  }
  .view-col img {
    max-width: 100%;
    max-height: 220px;
    object-fit: contain;
    border-radius: 4px;
    display: block;
    margin: 0 auto;
    background: #111;
  }
  .view-label {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-top: 4px;
    font-weight: 600;
  }
  .engine-stats {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    font-size: 0.8rem;
    background: #151f30;
    padding: 10px;
    border-radius: 6px;
  }
  .engine-box h4 {
    margin: 0 0 4px 0;
    font-size: 0.82rem;
    display: flex;
    justify-content: space-between;
  }
  .stat-line { color: var(--text-muted); display: flex; justify-content: space-between; }
  .stat-val { color: #fff; font-weight: 500; }
  .err-box {
    background: rgba(239, 68, 68, 0.15);
    color: #fca5a5;
    border-left: 3px solid var(--red);
    padding: 6px 8px;
    font-size: 0.75rem;
    border-radius: 2px;
    margin-top: 4px;
    word-break: break-word;
  }
</style>
<script>
function filterStyle(style) {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  const sections = document.querySelectorAll('.style-section');
  sections.forEach(s => {
    if (style === 'all' || s.dataset.style === style) {
      s.style.display = 'block';
    } else {
      s.style.display = 'none';
    }
  });
}
function filterUsableOnly() {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  document.querySelectorAll('.style-section').forEach(s => s.style.display = 'block');
  document.querySelectorAll('.card').forEach(c => {
    if (c.dataset.usable === 'true') {
      c.style.display = 'flex';
    } else {
      c.style.display = 'none';
    }
  });
}
</script>
</head>
<body>
<h1>Dataset Reality Check: Style Survey & Usability</h1>
<div class="subtitle">Evaluated on """
        + str(tot_all)
        + """ plans across classic and neural engines with apply=False. Usable draft criteria: &ge; 5 rooms, 5–30 m footprint per side, walls &le; 3 &times; rooms + 20.</div>

<table class="summary-table">
<thead>
  <tr>
    <th>Style</th>
    <th>Count</th>
    <th>Classic Usable Drafts</th>
    <th>Neural Usable Drafts</th>
    <th>Either Engine Usable</th>
  </tr>
</thead>
<tbody>
"""
    ]

    for s in styles:
        st = summary_stats[s]
        html_parts.append(
            f"""<tr>
  <td><strong>{style_labels.get(s, s)}</strong></td>
  <td>{st['total']} ({st['total'] / max(1, tot_all) * 100:.1f}%)</td>
  <td><span class="badge {('badge-usable' if st['classic_usable'] else 'badge-bad')}">{st['classic_usable']} / {st['total']} ({st['classic_pct']:.1f}%)</span></td>
  <td><span class="badge {('badge-usable' if st['neural_usable'] else 'badge-bad')}">{st['neural_usable']} / {st['total']} ({st['neural_pct']:.1f}%)</span></td>
  <td><span class="badge {('badge-usable' if st['either_usable'] else 'badge-bad')}">{st['either_usable']} / {st['total']} ({st['either_pct']:.1f}%)</span></td>
</tr>"""
        )

    tot = summary_stats["TOTAL"]
    html_parts.append(
        f"""<tr>
  <td>TOTAL</td>
  <td>{tot['total']} (100.0%)</td>
  <td><span class="badge {('badge-usable' if tot['classic_usable'] else 'badge-bad')}">{tot['classic_usable']} / {tot['total']} ({tot['classic_pct']:.1f}%)</span></td>
  <td><span class="badge {('badge-usable' if tot['neural_usable'] else 'badge-bad')}">{tot['neural_usable']} / {tot['total']} ({tot['neural_pct']:.1f}%)</span></td>
  <td><span class="badge {('badge-usable' if tot['either_usable'] else 'badge-bad')}">{tot['either_usable']} / {tot['total']} ({tot['either_pct']:.1f}%)</span></td>
</tr>
</tbody>
</table>

<div class="filter-bar">
  <button class="filter-btn active" onclick="filterStyle('all')">Show All ({tot_all})</button>
  <button class="filter-btn" onclick="filterStyle('line')">Line ({summary_stats['line']['total']})</button>
  <button class="filter-btn" onclick="filterStyle('grey-filled')">Grey-Filled ({summary_stats['grey-filled']['total']})</button>
  <button class="filter-btn" onclick="filterStyle('render')">Renders ({summary_stats['render']['total']})</button>
  <button class="filter-btn" onclick="filterStyle('solid')">Solid ({summary_stats['solid']['total']})</button>
  <button class="filter-btn" onclick="filterUsableOnly()">Usable Drafts Only ({tot['either_usable']})</button>
</div>
"""
    )

    for s in styles:
        s_rows = [r for r in rows if r["style"] == s]
        if not s_rows:
            continue
        st = summary_stats[s]
        html_parts.append(
            f"""<div class="style-section" data-style="{s}">
  <div class="style-header">
    <h2>{style_labels.get(s, s)} <span style="font-size:0.9rem; color:var(--text-muted); font-weight:normal;">({len(s_rows)} plans)</span></h2>
    <div>
      <span class="badge badge-style">Classic Usable: {st['classic_pct']:.1f}%</span>
      <span class="badge badge-style">Neural Usable: {st['neural_pct']:.1f}%</span>
    </div>
  </div>
  <div class="card-grid">
"""
        )
        for r in s_rows:
            c_use = str(r.get("classic_usable")).lower() == "true"
            n_use = str(r.get("neural_usable")).lower() == "true"
            any_use = "true" if (c_use or n_use) else "false"

            c_ov = r.get("classic_overlay") or ""
            n_ov = r.get("neural_overlay") or ""

            html_parts.append(
                f"""    <div class="card" data-usable="{any_use}">
      <div class="card-title">
        <span>{html.escape(r['stem'])}</span>
        <span class="badge badge-style">{s}</span>
      </div>
      <div class="views-row">
        <div class="view-col">
          {f'<img src="{c_ov}" loading="lazy" alt="Classic">' if c_ov else '<div style="padding:40px 0; color:#555">No Overlay</div>'}
          <div class="view-label">Classic Overlay</div>
        </div>
        <div class="view-col">
          {f'<img src="{n_ov}" loading="lazy" alt="Neural">' if n_ov else '<div style="padding:40px 0; color:#555">No Overlay</div>'}
          <div class="view-label">Neural Overlay</div>
        </div>
      </div>
      <div class="engine-stats">
        <div class="engine-box">
          <h4><span>Classic</span> <span class="badge {('badge-usable' if c_use else 'badge-bad')}">{'Usable' if c_use else 'No Draft'}</span></h4>
          {f'<div class="err-box">{html.escape(str(r.get("classic_error"))[:80])}</div>' if r.get("classic_error") else f'''
          <div class="stat-line"><span>Rooms:</span> <span class="stat-val">{r.get("classic_rooms")} ({r.get("classic_named_rooms")} named)</span></div>
          <div class="stat-line"><span>Walls:</span> <span class="stat-val">{r.get("classic_walls")}</span></div>
          <div class="stat-line"><span>Openings:</span> <span class="stat-val">{r.get("classic_openings")}</span></div>
          <div class="stat-line"><span>Footprint:</span> <span class="stat-val">{r.get("classic_footprint_w_m")} &times; {r.get("classic_footprint_d_m")} m</span></div>
          <div class="stat-line"><span>Time:</span> <span class="stat-val">{r.get("classic_seconds")}s</span></div>
          '''}
        </div>
        <div class="engine-box">
          <h4><span>Neural</span> <span class="badge {('badge-usable' if n_use else 'badge-bad')}">{'Usable' if n_use else 'No Draft'}</span></h4>
          {f'<div class="err-box">{html.escape(str(r.get("neural_error"))[:80])}</div>' if r.get("neural_error") else f'''
          <div class="stat-line"><span>Rooms:</span> <span class="stat-val">{r.get("neural_rooms")} ({r.get("neural_named_rooms")} named)</span></div>
          <div class="stat-line"><span>Walls:</span> <span class="stat-val">{r.get("neural_walls")}</span></div>
          <div class="stat-line"><span>Openings:</span> <span class="stat-val">{r.get("neural_openings")}</span></div>
          <div class="stat-line"><span>Footprint:</span> <span class="stat-val">{r.get("neural_footprint_w_m")} &times; {r.get("neural_footprint_d_m")} m</span></div>
          <div class="stat-line"><span>Time:</span> <span class="stat-val">{r.get("neural_seconds")}s</span></div>
          '''}
        </div>
      </div>
    </div>
"""
            )
        html_parts.append("  </div>\n</div>\n")

    html_parts.append("</body>\n</html>\n")
    out_path.write_text("".join(html_parts), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Style survey over floor plan images.")
    parser.add_argument("--images", default="models/Dataset", help="Directory containing images")
    parser.add_argument("--out", default="survey", help="Output directory for survey.csv, survey.html, overlays")
    parser.add_argument("--limit", type=int, default=None, help="Max images to process")
    parser.add_argument("--redo", action="store_true", help="Redo already processed images")
    args = parser.parse_args()

    images_dir = Path(args.images).expanduser()
    if not images_dir.is_dir():
        logger.error("Images directory not found: %s", images_dir)
        sys.exit(1)

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    overlays_dir = out_dir / "overlays"
    overlays_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "survey.csv"
    html_path = out_dir / "survey.html"

    # Find images
    all_images = sorted(list(images_dir.glob("*.jpg")) + list(images_dir.glob("*.png")))
    if args.limit:
        all_images = all_images[: args.limit]

    logger.info("Total images to process: %d", len(all_images))

    # Read existing rows if resuming
    existing_rows: Dict[str, Dict[str, Any]] = {}
    if csv_path.is_file() and not args.redo:
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_rows[row["stem"]] = row
        logger.info("Loaded %d existing rows from %s", len(existing_rows), csv_path)

    results: List[Dict[str, Any]] = []

    # Prepare CSV file
    csv_file = open(csv_path, "w", encoding="utf-8", newline="")
    csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDNAMES)
    csv_writer.writeheader()

    for idx, img_path in enumerate(all_images, start=1):
        stem = img_path.stem
        if stem in existing_rows and not args.redo:
            row = existing_rows[stem]
            results.append(row)
            csv_writer.writerow(row)
            csv_file.flush()
            continue

        logger.info("[%d/%d] Processing %s ...", idx, len(all_images), stem)
        style, stats = classify_style(str(img_path))

        # Run Classic
        c_overlay = overlays_dir / f"{stem}_classic.png"
        c_res = run_engine_trace(str(img_path), "classic", c_overlay)

        # Run Neural
        n_overlay = overlays_dir / f"{stem}_neural.png"
        n_res = run_engine_trace(str(img_path), "neural", n_overlay)

        row = {
            "stem": stem,
            "filename": img_path.name,
            "style": style,
            "pct_col": stats["pct_col"],
            "col_diff": stats["col_diff"],
            "pct_dark": stats["pct_dark"],
            "pct_grey": stats["pct_grey"],
            "classic_status": c_res["status"],
            "classic_error": c_res["error"],
            "classic_seconds": c_res["seconds"],
            "classic_rooms": c_res["rooms"],
            "classic_walls": c_res["walls"],
            "classic_openings": c_res["openings"],
            "classic_doors": c_res["doors"],
            "classic_windows": c_res["windows"],
            "classic_named_rooms": c_res["named_rooms"],
            "classic_unnamed_rooms": c_res["unnamed_rooms"],
            "classic_scale_from": c_res["scale_from"],
            "classic_scale_mm_per_px": c_res["scale_mm_per_px"],
            "classic_footprint_w_m": c_res["footprint_w_m"],
            "classic_footprint_d_m": c_res["footprint_d_m"],
            "classic_usable": c_res["usable"],
            "classic_overlay": c_res["overlay"],
            "neural_status": n_res["status"],
            "neural_error": n_res["error"],
            "neural_seconds": n_res["seconds"],
            "neural_rooms": n_res["rooms"],
            "neural_walls": n_res["walls"],
            "neural_openings": n_res["openings"],
            "neural_doors": n_res["doors"],
            "neural_windows": n_res["windows"],
            "neural_named_rooms": n_res["named_rooms"],
            "neural_unnamed_rooms": n_res["unnamed_rooms"],
            "neural_scale_from": n_res["scale_from"],
            "neural_scale_mm_per_px": n_res["scale_mm_per_px"],
            "neural_footprint_w_m": n_res["footprint_w_m"],
            "neural_footprint_d_m": n_res["footprint_d_m"],
            "neural_usable": n_res["usable"],
            "neural_overlay": n_res["overlay"],
        }
        results.append(row)
        csv_writer.writerow(row)
        csv_file.flush()

        logger.info(
            "   [%s] Classic: rooms=%d walls=%d usable=%s (%.1fs) | Neural: rooms=%d walls=%d usable=%s (%.1fs)",
            style,
            c_res["rooms"],
            c_res["walls"],
            c_res["usable"],
            c_res["seconds"],
            n_res["rooms"],
            n_res["walls"],
            n_res["usable"],
            n_res["seconds"],
        )

    csv_file.close()
    logger.info("Wrote %s", csv_path)

    # Copy to ./survey.csv in root as well for convenience
    try:
        import shutil

        shutil.copyfile(csv_path, _ROOT / "survey.csv")
    except Exception:
        pass

    render_html_report(results, html_path)
    logger.info("Wrote %s", html_path)
    try:
        shutil.copyfile(html_path, _ROOT / "survey.html")
    except Exception:
        pass

    # Print summary table
    styles = ["line", "grey-filled", "render", "solid"]
    print("\n" + "=" * 70)
    print("STYLE SURVEY SUMMARY:")
    print("=" * 70)
    print(f"{'Style':<15} {'Total':<8} {'Classic Usable':<18} {'Neural Usable':<18} {'Either Usable':<18}")
    print("-" * 70)
    for s in styles:
        s_rows = [r for r in results if r["style"] == s]
        tot = len(s_rows)
        c_use = sum(1 for r in s_rows if str(r.get("classic_usable")).lower() == "true")
        n_use = sum(1 for r in s_rows if str(r.get("neural_usable")).lower() == "true")
        e_use = sum(
            1 for r in s_rows
            if str(r.get("classic_usable")).lower() == "true" or str(r.get("neural_usable")).lower() == "true"
        )
        c_pct = (c_use / tot * 100) if tot else 0
        n_pct = (n_use / tot * 100) if tot else 0
        e_pct = (e_use / tot * 100) if tot else 0
        print(f"{s:<15} {tot:<8} {f'{c_use} ({c_pct:.1f}%)':<18} {f'{n_use} ({n_pct:.1f}%)':<18} {f'{e_use} ({e_pct:.1f}%)':<18}")

    tot_all = len(results)
    tot_c = sum(1 for r in results if str(r.get("classic_usable")).lower() == "true")
    tot_n = sum(1 for r in results if str(r.get("neural_usable")).lower() == "true")
    tot_e = sum(
        1 for r in results
        if str(r.get("classic_usable")).lower() == "true" or str(r.get("neural_usable")).lower() == "true"
    )
    print("-" * 70)
    print(
        f"{'TOTAL':<15} {tot_all:<8} {f'{tot_c} ({tot_c/max(1,tot_all)*100:.1f}%)':<18} {f'{tot_n} ({tot_n/max(1,tot_all)*100:.1f}%)':<18} {f'{tot_e} ({tot_e/max(1,tot_all)*100:.1f}%)':<18}"
    )
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
