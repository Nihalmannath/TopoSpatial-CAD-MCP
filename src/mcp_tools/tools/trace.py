"""``trace_plan``: raster floor plan -> native walls, doors, windows, rooms.

One call does the whole job: recognise the image (about 1 s), compile the
trace into a ``manage_design`` change batch, preview + apply it as a single
transaction (so ``manage_design rollback`` undoes it), then draw room
outlines and labels. An overlay PNG of what was recognised is saved next to
the drawings for checking.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class RoomSize(BaseModel):
    """A room name and its printed clear size, e.g. KITCHEN 320X307 (cm)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    width_cm: float = Field(gt=0)
    depth_cm: float = Field(gt=0)


def _fail(stage: str, details: Any, suggested: str) -> str:
    return json.dumps(
        {"success": False, "error": {"stage": stage, "details": details, "suggested_action": suggested}},
        ensure_ascii=False,
    )


def register_trace_tools(mcp: Any) -> None:
    """Register the one-shot ``trace_plan`` tool."""

    @mcp.tool()
    def trace_plan(
        image_path: str,
        room_sizes: Optional[List[RoomSize]] = None,
        mm_per_px: Optional[float] = None,
        nominal_wall_mm: float = 230.0,
        insertion_point: Optional[List[float]] = None,
        apply: bool = True,
        draw_rooms: bool = True,
        engine: str = "auto",
        weights_path: Optional[str] = None,
        geometry_pipeline: Optional[str] = None,
        rooms_in_transaction: bool = False,
        wall_logit_bias: Optional[float] = None,
    ) -> str:
        """
        Trace a floor-plan image (PNG/JPG) into the current drawing in one call.

        Recognises walls (with thickness), doors and windows (gaps in walls;
        glazed gaps are windows), open-plan room separations and thin drawn
        partitions, then creates native walls/doors/windows through a single
        preview+apply design transaction and draws named room outlines.

        Scale, best first:
        - ``room_sizes``: the room names and printed sizes you can read on the
          plan, e.g. [{"name": "Kitchen", "width_cm": 320, "depth_cm": 307}].
          They name the rooms and fix the scale; no position is needed.
        - automatic OCR of "NAME / 320X307" labels when Tesseract is installed;
        - ``mm_per_px`` if known;
        - otherwise walls are assumed ``nominal_wall_mm`` thick.

        ``engine``: "raster2seq" (trained vector polygon model with automatic
        checkpoint quality fallback), "classic" (solid dark walls, no extra
        installs), "neural"
        (CubiCasa5K network: outline walls, cluttered plans; needs PyTorch and
        model_best_val_loss_var.pkl in Documents/TopoSpatial Exports/models),
        or "auto": classic first, neural when classic reads the plan poorly.

        ``geometry_pipeline``: "legacy" (shipped behaviour) or "canonical" (one
        mm frame, lineage-based opening hosting, small gap repair,
        exposure-resolved thickness, structured diagnostics). Default: the
        promoted setting in models/neural_profile.json for the neural engine
        (canonical), legacy for the other engines. Engines
        are chosen by observable quality indicators (dangling ends, enclosed
        area, openings placed, agreement with room_sizes), reported under
        "quality".

        ``wall_logit_bias`` (neural only): > 0 draws more walls (thin
        partitions), < 0 fewer. Default: the promoted value for the promoted
        weights (2.0 for the pretrained CubiCasa weights), else 0.

        ``rooms_in_transaction``: put the room boundaries into the same
        manage_design transaction as the walls (so rollback removes them too)
        instead of drawing outline polylines afterwards. Needs the boundary
        room support in the topology bridge.

        ``apply=False`` only recognises and returns the summary + overlay.
        Undo the whole trace with manage_design rollback (walls/openings).
        """
        started = time.perf_counter()
        try:
            from trace_engine import compile_trace
            from trace_engine.vision import render_overlay, trace_image
        except Exception as exc:  # pragma: no cover - environment problem
            return _fail("import", str(exc), "Install numpy, scipy and pillow in the server environment.")

        path = Path(image_path).expanduser()
        if not path.is_file():
            return _fail("input", f"image not found: {path}", "Pass an absolute path to a PNG or JPG on this computer.")

        sizes = [(r.name, r.width_cm, r.depth_cm) for r in (room_sizes or [])]
        from core import get_config  # noqa: WPS433 - server-only import
        from trace_engine.neural import status as neural_status
        from trace_engine.raster2seq_inference import status as raster2seq_status

        models_dir = str(Path(get_config().output.directory).expanduser() / "models")
        raster_requested = engine in {"raster2seq", "r2seq"}
        neural_ok, neural_info = neural_status(None if raster_requested else weights_path, [models_dir])
        raster_ok, raster_info = raster2seq_status(weights_path if raster_requested else None)
        if engine not in {"auto", "classic", "neural", "raster2seq", "r2seq"}:
            return _fail("engine", f"unsupported engine: {engine}",
                         "Use auto, raster2seq, classic, or neural.")
        if geometry_pipeline not in {None, "legacy", "canonical"}:
            return _fail("geometry_pipeline", f"unsupported geometry_pipeline: {geometry_pipeline}",
                         "Use legacy or canonical.")
        if engine == "neural" and not neural_ok:
            return _fail("engine", neural_info,
                         "Install PyTorch in the server venv and put model_best_val_loss_var.pkl in " + models_dir)
        if raster_requested and not raster_ok:
            return _fail("engine", raster_info,
                         "Install the private Raster2Seq runtime and a verified checkpoint.")
        id_prefix = f"trace-{path.stem[:24].replace(' ', '_')}-{int(time.time()) % 100000}"
        from trace_engine.neural import load_profile

        profile = load_profile()

        def pipeline_for(which: str) -> str:
            if geometry_pipeline:
                return geometry_pipeline
            if which == "neural" and profile.get("geometry_pipeline") in ("legacy", "canonical"):
                return profile["geometry_pipeline"]
            return "legacy"

        def run(which: str):
            if which == "raster2seq":
                from trace_engine.raster2seq_inference import trace_image as trace_raster2seq

                tr, rep = trace_raster2seq(
                    str(path),
                    mm_per_px=mm_per_px or 10.0,
                    nominal_wall_mm=nominal_wall_mm,
                    weights_path=weights_path if raster_requested else None,
                )
            else:
                tr, rep = trace_image(
                    str(path), mm_per_px=mm_per_px, nominal_wall_mm=nominal_wall_mm,
                    room_sizes=sizes or None, engine=which,
                    weights=neural_info if which == "neural" else None,
                    wall_logit_bias=wall_logit_bias if which == "neural" else 0.0,
                )
            pipeline = pipeline_for(which)
            if pipeline == "canonical":
                from trace_engine.models import CoordinateFrame

                method = str(getattr(rep, "scale_method", "") or "")
                tr = tr.model_copy(update={"frame": CoordinateFrame(
                    calibrated=not method.startswith(("nominal", "default")), scale_source=method or None)})
            comp = compile_trace(
                tr, representation="auto",
                insertion_point=tuple(insertion_point[:2]) if insertion_point else (0.0, 0.0),
                id_prefix=id_prefix, geometry_pipeline=pipeline,
            )
            from trace_engine.quality import assess

            comp.quality = assess(comp, sizes or None, len(tr.doors_list) + len(tr.windows_list))
            return tr, rep, comp

        def quality(comp) -> float:
            """Heuristic engine score from observable indicators (see trace_engine.quality)."""
            return float(comp.quality["score"])

        attempts, errors = [], []
        if raster_requested:
            order = ["raster2seq"]
        elif engine == "neural":
            order = ["neural"]
        elif engine == "classic":
            order = ["classic"]
        else:
            order = (["raster2seq"] if raster_ok else []) + ["classic"]
            if neural_ok:
                order.append("neural")
        for which in order:
            if attempts and quality(attempts[-1][3]) >= 0.75:
                break
            try:
                attempts.append((which, *run(which)))
            except Exception as exc:
                logger.exception("%s trace failed", which)
                errors.append(f"{which}: {exc}")
        if not attempts:
            hint = "" if neural_ok else f" Neural engine unavailable: {neural_info}."
            return _fail("recognise", errors, "Check the image is a floor plan." + hint)
        used, trace, report, compiled = max(attempts, key=lambda a: quality(a[3]))
        recognised_s = round(time.perf_counter() - started, 2)

        out_dir = Path(get_config().output.directory).expanduser() / "traces"
        out_dir.mkdir(parents=True, exist_ok=True)
        overlay = out_dir / f"{path.stem}_trace.png"
        try:
            render_overlay(str(path), trace, compiled, str(overlay))
        except Exception as exc:  # overlay is a convenience only
            logger.warning("overlay failed: %s", exc)
            overlay = None
        try:  # pixel <-> drawing transform, used to turn corrections into training data
            from PIL import Image

            with Image.open(path) as im:
                size = list(im.size)
            (out_dir / f"{path.stem}_align.json").write_text(json.dumps({
                "image": str(path), "image_size": size, "mm_per_px": report.mm_per_px,
                "offset_mm": list(compiled.offset), "y_down": True, "engine": used,
            }), encoding="utf-8")
        except Exception as exc:
            logger.warning("alignment sidecar not written: %s", exc)
        (out_dir / f"{path.stem}_trace.json").write_text(
            json.dumps(trace.model_dump(by_alias=True)), encoding="utf-8"
        )

        summary = compiled.summary()
        result: Dict[str, Any] = {
            "success": True,
            "engine": used,
            "geometry_pipeline": compiled.pipeline,
            "neural_settings": ({"weights": neural_info, "wall_logit_bias": getattr(report, "wall_logit_bias", None),
                                 "profile": bool(profile)} if used == "neural" else None),
            "calibration": {"mm_per_px": round(report.mm_per_px, 4), "source": report.scale_method,
                            "calibrated": not str(report.scale_method).startswith(("nominal", "default"))},
            "quality": compiled.quality,
            "engines_tried": {a[0]: round(quality(a[3]), 2) for a in attempts},
            "neural_available": neural_ok if neural_ok else neural_info,
            "raster2seq_available": raster_ok if raster_ok else raster_info,
            "recognised_in_s": recognised_s,
            "scale_mm_per_px": round(report.mm_per_px, 3),
            "scale_from": report.scale_method,
            "wall_thickness_mm": round(report.wall_thickness_px * report.mm_per_px),
            "counts": summary["counts"],
            "footprint_mm": summary["footprint_mm"],
            "rooms": summary["rooms"],
            "warnings": summary["warnings"][:12] + report.warnings + errors,
            "overlay_png": str(overlay) if overlay else None,
        }
        if compiled.diagnostics:
            result["diagnostic_counts"] = summary["diagnostic_counts"]
            result["diagnostics"] = [d for d in compiled.diagnostics if d["severity"] != "info"][:20]
        if used == "raster2seq":
            result["checkpoint"] = report.checkpoint
            result["checkpoint_quality"] = report.candidate_quality
            result["crop_box"] = list(report.crop_box) if report.crop_box else None
        if not apply or not any(c.get("@type") == "top:Wall" for c in compiled.changes):
            result["applied"] = False
            return json.dumps(result, ensure_ascii=False)

        # ---- apply as one design transaction (rooms drawn separately below)
        from adapters.adapter_manager import get_adapter
        from adapters.com_worker import run_com
        from design_engine.models import InspectDesignRequest, PlanDesignRequest, TransactionDesignRequest
        from mcp_tools.tools.design import _orchestrator

        adapter = get_adapter(None, only_if_running=True)
        building = [c for c in compiled.changes if rooms_in_transaction or c["@type"] != "top:Room"]
        inspected = _orchestrator.execute(InspectDesignRequest(action="inspect"), adapter)
        if not inspected.get("success"):
            return _fail("inspect", inspected.get("error"), "Open a drawing in AutoCAD and retry.")
        preview = _orchestrator.execute(
            PlanDesignRequest(action="preview", base_revision=inspected["revision"], changes=building), adapter
        )
        if not preview.get("success"):
            result.update(applied=False, error=preview.get("error"))
            return json.dumps(result, ensure_ascii=False)
        applied = _orchestrator.execute(
            TransactionDesignRequest(action="apply", transaction_id=preview["transaction_id"]), adapter
        )
        if not applied.get("success"):
            result.update(applied=False, error=applied.get("error"))
            return json.dumps(result, ensure_ascii=False)
        result["applied"] = True
        result["preview_summary"] = {k: preview.get(k) for k in ("summary", "counts", "warnings") if k in preview}
        result["transaction_id"] = preview["transaction_id"]
        result["revision"] = (applied.get("result") or {}).get("revision")

        if draw_rooms and not rooms_in_transaction:
            rooms = [c for c in compiled.changes if c["@type"] == "top:Room"]

            def _draw() -> int:
                cad = get_adapter(None, only_if_running=True)
                count = 0
                handles: List[str] = []
                try:
                    for room in rooms:
                        pts = [tuple(p) for p in room["geometry"]["boundary"]]
                        handles.append(cad.draw_polyline(pts, True, "A-AREA", "green", 0, _skip_refresh=True))
                        xs = [p[0] for p in pts]
                        ys = [p[1] for p in pts]
                        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
                        w, d = (max(xs) - min(xs)) / 10, (max(ys) - min(ys)) / 10
                        name = room["label"].upper()
                        handles.append(cad.draw_text((cx - 55 * len(name), cy + 60), name, 200, 0, "A-ANNO", "white",
                                                     _skip_refresh=True))
                        size = f"{w:.0f}X{d:.0f}"
                        handles.append(cad.draw_text((cx - 45 * len(size), cy - 240), size, 150, 0, "A-ANNO", "cyan",
                                                     _skip_refresh=True))
                        count += 1
                except Exception:
                    # All-or-nothing: remove the outlines/labels already drawn.
                    for handle in handles:
                        if handle:
                            cad.delete_entity(handle)
                    raise
                try:
                    cad.zoom_extents()
                except Exception:
                    pass
                return count

            try:
                result["rooms_drawn"] = run_com(_draw, cad_type="autocad", timeout=120)
            except Exception as exc:
                result["rooms_drawn"] = 0
                result["warnings"].append(f"room outlines not drawn: {exc}")
        result["total_s"] = round(time.perf_counter() - started, 2)
        return json.dumps(result, ensure_ascii=False)


_ROOM_LAYERS = {"A-AREA", "AI-ROOMS"}


def _collect_drawing(doc: Any) -> Any:
    """Read walls, openings, room outlines and texts from model space."""
    from trace_engine.dataset import DrawingContent

    content = DrawingContent()
    for ent in doc.ModelSpace:
        try:
            kind = str(ent.ObjectName)
            if kind == "AecDbWall":
                s, e = ent.StartPoint, ent.EndPoint
                content.walls.append(((s[0], s[1]), (e[0], e[1]), float(ent.Width)))
            elif kind == "AcDbLine" and ("WALL" in str(getattr(ent, "Layer", "")).upper()):
                s, e = ent.StartPoint, ent.EndPoint
                content.walls.append(((s[0], s[1]), (e[0], e[1]), 230.0))
            elif kind in ("AecDbDoor", "AecDbWindow"):
                lo, hi = ent.GetBoundingBox()
                content.openings.append(("door" if kind == "AecDbDoor" else "window", (lo[0], lo[1]), (hi[0], hi[1])))
            elif kind == "AcDbPolyline" and str(ent.Layer).upper() in _ROOM_LAYERS and ent.Closed:
                co = list(ent.Coordinates)
                content.rooms.append(list(zip(co[0::2], co[1::2])))
            elif kind in ("AcDbText", "AcDbMText"):
                ip = ent.InsertionPoint
                content.texts.append((str(ent.TextString), (ip[0], ip[1])))
        except Exception:  # one odd entity must not stop the export
            continue
    return content


def register_training_tools(mcp: Any) -> None:
    """Register ``export_training_example`` and ``prepare_labeling_drawing``."""

    @mcp.tool()
    def prepare_labeling_drawing(image_path: str, mm_per_px: Optional[float] = None) -> str:
        """
        Create a new drawing, insert the image as a raster underlay (AddRaster)
        scaled by mm_per_px (from OCR sizes if found, else nominal), with its
        top-left at the same transform trace_plan uses, on a locked layer "TRACE-IMAGE".
        Write <stem>_align.json in the same format, so export_training_example
        works for walls drawn from scratch.
        """
        import pythoncom
        import win32com.client
        from PIL import Image
        from adapters.adapter_manager import get_adapter
        from adapters.com_worker import run_com
        from core import get_config
        from trace_engine.compiler import compile_trace
        from trace_engine.dataset import Alignment
        from trace_engine.vision import trace_image

        path = Path(image_path).expanduser().resolve()
        if not path.is_file():
            return _fail("input", f"image not found: {path}", "Pass a valid image path.")

        try:
            with Image.open(path) as im:
                w_px, h_px = im.size
        except Exception as exc:
            return _fail("input", f"cannot open image: {exc}", "Check image format.")

        root = Path(get_config().output.directory).expanduser()
        out_dir = root / "traces"
        out_dir.mkdir(parents=True, exist_ok=True)
        align_file = out_dir / f"{path.stem}_align.json"

        # Determine scale and transform (offset)
        scale: Optional[float] = None
        scale_method = "nominal"
        offset_mm: Optional[Tuple[float, float]] = None

        if mm_per_px is not None and mm_per_px > 0:
            scale = float(mm_per_px)
            scale_method = "user specified"
        elif align_file.is_file():
            try:
                existing = Alignment.load(align_file)
                scale = existing.mm_per_px
                offset_mm = existing.offset_mm
                scale_method = f"existing {align_file.name}"
            except Exception:
                pass

        if scale is None or offset_mm is None:
            try:
                trace, report = trace_image(str(path), ocr=True, engine="classic")
                if scale is None:
                    scale = float(trace.mm_per_unit)
                    scale_method = report.scale_method
                if offset_mm is None:
                    if trace.walls_graph.nodes:
                        compiled = compile_trace(trace)
                        offset_mm = tuple(compiled.offset)
                    else:
                        offset_mm = (0.0, round(h_px * scale, 1))
            except Exception as exc:
                logger.warning("trace_image in prepare_labeling_drawing failed: %s", exc)
                if scale is None:
                    scale = 10.0
                    scale_method = "default nominal 10.0 mm/px"
                if offset_mm is None:
                    offset_mm = (0.0, round(h_px * scale, 1))

        # Write <stem>_align.json
        align_data = {
            "image": str(path),
            "image_size": [w_px, h_px],
            "mm_per_px": round(scale, 4),
            "offset_mm": [round(offset_mm[0], 2), round(offset_mm[1], 2)],
            "y_down": True,
            "engine": "labeling",
        }
        align_file.write_text(json.dumps(align_data, indent=2), encoding="utf-8")

        # In AutoCAD, AddRaster insertion point is lower-left
        # Top-left of image in CAD is (offset_mm[0], offset_mm[1])
        # Lower-left of image in CAD is (offset_mm[0], offset_mm[1] - h_px * scale)
        ll_x = float(offset_mm[0])
        ll_y = float(offset_mm[1] - h_px * scale)
        scale_factor = float(w_px * scale)

        def _insert() -> Dict[str, Any]:
            cad = get_adapter(None, only_if_running=False, allow_launch=True)
            app = cad.application
            doc = app.Documents.Add()
            try:
                doc.Activate()
            except Exception:
                pass
            cad.document = doc

            # Create standard architectural labeling layers
            for lyr in ["AI-WALLS", "A-WALL", "A-AREA", "A-ANNO"]:
                try:
                    doc.Layers.Item(lyr)
                except Exception:
                    try:
                        doc.Layers.Add(lyr)
                    except Exception:
                        pass

            try:
                layer = doc.Layers.Item("TRACE-IMAGE")
            except Exception:
                layer = doc.Layers.Add("TRACE-IMAGE")
            layer.Lock = False

            pt = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [ll_x, ll_y, 0.0])
            raster = doc.ModelSpace.AddRaster(str(path), pt, scale_factor, 0.0)
            raster.Layer = "TRACE-IMAGE"

            try:
                doc.ActiveLayer = doc.Layers.Item("0")
            except Exception:
                pass
            layer.Lock = True

            try:
                app.ZoomExtents()
            except Exception:
                pass

            return {
                "drawing_name": str(doc.Name),
                "raster_handle": str(raster.Handle),
            }

        try:
            cad_res = run_com(_insert, cad_type="autocad", timeout=120)
        except Exception as exc:
            return _fail("cad", str(exc), "Ensure AutoCAD is open and responsive.")

        info = {
            "success": True,
            "drawing": cad_res["drawing_name"],
            "image_path": str(path),
            "image_size": [w_px, h_px],
            "scale_mm_per_px": round(scale, 4),
            "scale_from": scale_method,
            "offset_mm": [round(offset_mm[0], 2), round(offset_mm[1], 2)],
            "layer": "TRACE-IMAGE",
            "layer_locked": True,
            "align_json": str(align_file),
        }
        return json.dumps(info, ensure_ascii=False)

    @mcp.tool()
    def export_training_example(image_path: str, name: Optional[str] = None) -> str:
        """
        Save the CURRENT drawing as a training example for the neural tracer.

        Use after ``trace_plan`` on ``image_path`` and after the user has
        corrected the walls, doors, windows and room outlines/names in AutoCAD.
        Reads AecDbWall/AecDbDoor/AecDbWindow, closed polylines on A-AREA and
        room-name texts, and rasterises them onto the image using the
        ``<image>_align.json`` written by trace_plan (do not move the drawing
        between tracing and export). Writes image/rooms/icons/preview PNGs to
        Documents/TopoSpatial Exports/dataset/<name>/.
        """
        from adapters.adapter_manager import get_adapter
        from adapters.com_worker import run_com
        from core import get_config
        from trace_engine.dataset import Alignment, write_example

        path = Path(image_path).expanduser()
        root = Path(get_config().output.directory).expanduser()
        align_file = root / "traces" / f"{path.stem}_align.json"
        if not path.is_file():
            return _fail("input", f"image not found: {path}", "Pass the same image path used for trace_plan.")
        if not align_file.is_file():
            return _fail("alignment", f"missing {align_file}", "Run trace_plan on this image first (it writes the alignment).")
        align = Alignment.load(align_file)

        def _read() -> Any:
            cad = get_adapter(None, only_if_running=False, allow_launch=True)
            return _collect_drawing(cad._get_document("export training example"))

        try:
            content = run_com(_read, cad_type="autocad", timeout=180)
        except Exception as exc:
            return _fail("read", str(exc), "Open the corrected drawing in AutoCAD and retry.")
        if not content.walls:
            return _fail("read", "no native walls in the current drawing", "Switch to the traced drawing tab.")
        info = write_example(content, align, str(path), root / "dataset", name)
        examples = sum(1 for p in (root / "dataset").iterdir() if (p / "rooms.png").is_file())
        info.update(success=True, dataset_size=examples,
                    preview_png=str(Path(info["folder"]) / "preview.png"))
        return json.dumps(info, ensure_ascii=False)


_register_trace_plan = register_trace_tools


def register_trace_tools(mcp: Any) -> None:  # noqa: F811 - server imports this name
    """Register ``trace_plan``, ``prepare_labeling_drawing``, and ``export_training_example``."""
    _register_trace_plan(mcp)
    register_training_tools(mcp)
