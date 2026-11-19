"""Floor-plan tracing: recogniser output -> clean wall graph -> CAD changes.

Pipeline:

1. a recogniser (``vision.trace_image`` or any neural model) produces a
   ``TraceResult``: wall centerline graph, hosted doors/windows, room labels;
2. ``graph.clean`` / ``geometry_kernel.clean_walls_graph`` merges near-duplicate nodes, snaps axes, splits walls at
   junctions and joins straight runs;
3. ``rooms.recover_rooms`` / ``geometry_kernel.solve_rooms_from_walls`` finds rooms as closed wall loops, insets them to
   clear interiors and names them from label seed points;
4. ``compiler.compile_trace`` emits one ``manage_design`` change batch with
   walls, rooms and wall-hosted openings for preview/apply.

Canonical pipeline (``compile_trace(..., geometry_pipeline="canonical")``):
``canonical.normalize`` (one mm, y-up frame) -> ``regularize.regularize_trace``
(cleanup with edge lineage, bounded gap repair, faces, exposure via
``geometry_kernel.solve_wall_exposure``, thickness resolution, lineage-based
opening hosting, rooms with label points and persistent ids, diagnostics) ->
``compiler.compile_regularized``. ``quality.assess`` gives observable quality
indicators. The legacy path stays the default until the canonical path passes
the evaluation gates (docs/CANONICAL_PIPELINE.md).
"""

from .canonical import CanonicalPlan, Frame, normalize, to_trace_result
from .compiler import CompiledTrace, compile_regularized, compile_trace
# geometry_kernel needs shapely; keep the package importable without it.
try:
    from .geometry_kernel import (
        DEFAULT_WALL_THICKNESS,
        OPENING_STANDARDS,
        CoordinateProjector,
        PlanGeometryTracer,
        clean_walls_graph,
        compute_face_content_hash,
        host_openings_on_walls,
        inscribe_obb_furniture,
        solve_rooms_from_walls,
        solve_wall_exposure,
        solve_wall_fills,
    )
except ImportError:  # pragma: no cover - optional dependency
    pass
from .models import (
    SCHEMA_VERSION,
    CoordinateFrame,
    Diagnostic,
    FurnitureItem,
    TraceEdge,
    TraceNode,
    TraceOpening,
    TraceResult,
    TraceZone,
    WallsGraph,
    upgrade_trace,
)
from .quality import assess as assess_quality

try:
    from .regularize import RegularizeConfig, RegularizedPlan, regularize_trace
except ImportError:  # pragma: no cover - regularize imports geometry_kernel (numpy)
    pass

__all__ = [
    "SCHEMA_VERSION",
    "CanonicalPlan",
    "CompiledTrace",
    "CoordinateFrame",
    "Diagnostic",
    "Frame",
    "FurnitureItem",
    "RegularizeConfig",
    "RegularizedPlan",
    "assess_quality",
    "compile_regularized",
    "normalize",
    "regularize_trace",
    "to_trace_result",
    "upgrade_trace",
    "CoordinateProjector",
    "DEFAULT_WALL_THICKNESS",
    "OPENING_STANDARDS",
    "PlanGeometryTracer",
    "TraceEdge",
    "TraceNode",
    "TraceOpening",
    "TraceResult",
    "TraceZone",
    "WallsGraph",
    "clean_walls_graph",
    "compile_trace",
    "compute_face_content_hash",
    "host_openings_on_walls",
    "inscribe_obb_furniture",
    "solve_rooms_from_walls",
    "solve_wall_exposure",
    "solve_wall_fills",
]
