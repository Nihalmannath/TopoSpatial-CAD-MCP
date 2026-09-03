import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Arc, Circle, Group, Layer, Line, Rect, Stage, Text } from "react-konva";
import type { KonvaEventObject } from "konva/lib/Node";
import { inferRoomZone, ZONE_COLORS } from "./archUtils";
import type { ActiveTool, CadPlanEntity, EditorAction, EditorCommand, GraphNode, Point, StudioMode, TopologyChange } from "./types";

interface Props {
  nodes: GraphNode[];
  planEntities?: CadPlanEntity[];
  selectedId: string | null;
  failedIds: Set<string>;
  mode?: StudioMode;
  advancedGeometry?: boolean;
  onSelect: (id: string | null) => void;
  onCommitAction?: (action: EditorAction) => void;
}

const SPACE_TYPES = new Set(["top:Room", "top:Space"]);
const PORTAL_TYPES = new Set(["top:Door", "top:Opening", "top:Window"]);

function parsePoints(value: unknown): Point[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is Point =>
          Array.isArray(item) && item.length >= 2 && Number.isFinite(item[0]) && Number.isFinite(item[1]),
      )
    : [];
}

function dist(p1: Point, p2: Point): number {
  return Math.hypot(p2[0] - p1[0], p2[1] - p1[1]);
}

interface WallEntity {
  id: string;
  node: GraphNode;
  start: Point;
  end: Point;
  thickness: number;
  length: number;
  boundingRooms: string[];
}

export function PlanCanvas({ nodes, planEntities = [], selectedId, failedIds, mode = "current_drawing", advancedGeometry = false, onSelect, onCommitAction }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<any>(null);
  const [stageSize, setStageSize] = useState({ width: 900, height: 650 });

  // Pan & Zoom Stage Transform (in screen pixels relative to world coordinates in mm)
  const [zoom, setZoom] = useState(0.06); // pixels per mm (0.06 = 60px/meter)
  const [pan, setPan] = useState<Point>([400, 320]);

  // Cursor Real-World Coordinates
  const [cursorWorld, setCursorWorld] = useState<Point>([0, 0]);

  // Active Tool
  const [activeTool, setActiveTool] = useState<ActiveTool>("select");

  // Drafting settings
  const [snapGrid, setSnapGrid] = useState(true);
  const [gridSizeMm] = useState(100); // 100mm snap
  const [snapJunction, setSnapJunction] = useState(true);
  const [orthoMode, setOrthoMode] = useState(false);
  const [showDimensions, setShowDimensions] = useState(true);
  const [showRoomTags, setShowRoomTags] = useState(true);

  // Measure Tape State
  const [measureStart, setMeasureStart] = useState<Point | null>(null);
  const [measureCurrent, setMeasureCurrent] = useState<Point | null>(null);
  const [measureResult, setMeasureResult] = useState<{ start: Point; end: Point; lengthM: number } | null>(null);

  // Measure container size
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.contentRect.width > 1 && entry.contentRect.height > 1) {
          setStageSize({ width: entry.contentRect.width, height: entry.contentRect.height });
        }
      }
    });
    observer.observe(el);
    if (el.clientWidth > 1 && el.clientHeight > 1) {
      setStageSize({ width: el.clientWidth, height: el.clientHeight });
    }
    return () => observer.disconnect();
  }, []);

  // Compute World Bounds
  const bounds = useMemo(() => {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    let count = 0;
    for (const node of nodes) {
      const shape = (node["cad:geometry"] || {}) as Record<string, unknown>;
      const boundary = parsePoints(shape.boundary);
      for (const p of boundary) {
        minX = Math.min(minX, p[0]); maxX = Math.max(maxX, p[0]);
        minY = Math.min(minY, p[1]); maxY = Math.max(maxY, p[1]);
        count++;
      }
      for (const key of ["start", "end"]) {
        const p = shape[key];
        if (Array.isArray(p) && p.length >= 2 && Number.isFinite(p[0]) && Number.isFinite(p[1])) {
          minX = Math.min(minX, p[0]); maxX = Math.max(maxX, p[0]);
          minY = Math.min(minY, p[1]); maxY = Math.max(maxY, p[1]);
          count++;
        }
      }
    }
    for (const entity of planEntities) {
      const shape = entity.geometry || {};
      const values: unknown[] = [];
      if (shape.kind === "line" || shape.kind === "dimension") values.push(shape.start, shape.end);
      if (shape.kind === "dimension") values.push(shape.text_position);
      if (shape.kind === "polyline" && Array.isArray(shape.vertices)) values.push(...shape.vertices);
      if (shape.kind === "point") values.push(shape.position);
      if ((shape.kind === "arc" || shape.kind === "circle") && Array.isArray(shape.center)) {
        const radius = Number(shape.radius || 0);
        values.push(
          [Number(shape.center[0]) - radius, Number(shape.center[1]) - radius],
          [Number(shape.center[0]) + radius, Number(shape.center[1]) + radius],
        );
      }
      for (const value of values) {
        if (Array.isArray(value) && value.length >= 2 && Number.isFinite(value[0]) && Number.isFinite(value[1])) {
          minX = Math.min(minX, Number(value[0])); maxX = Math.max(maxX, Number(value[0]));
          minY = Math.min(minY, Number(value[1])); maxY = Math.max(maxY, Number(value[1]));
          count++;
        }
      }
    }
    if (count === 0) return { minX: 0, maxX: 12000, minY: 0, maxY: 9000, cx: 6000, cy: 4500, w: 12000, h: 9000 };
    const w = Math.max(1, maxX - minX);
    const h = Math.max(1, maxY - minY);
    return { minX, maxX, minY, maxY, cx: (minX + maxX) / 2, cy: (minY + maxY) / 2, w, h };
  }, [nodes, planEntities]);

  // Fit view to geometry bounds
  const fitView = useCallback(() => {
    const margin = 80;
    const availableW = Math.max(100, stageSize.width - margin * 2);
    const availableH = Math.max(100, stageSize.height - margin * 2);
    const newZoom = Math.min(availableW / bounds.w, availableH / bounds.h, 100);
    const clampedZoom = Math.max(0.0001, newZoom);
    setZoom(clampedZoom);
    setPan([
      stageSize.width / 2 - bounds.cx * clampedZoom,
      stageSize.height / 2 + bounds.cy * clampedZoom,
    ]);
  }, [bounds, stageSize]);

  // Initial fit
  const lastFitSize = useRef("");
  useEffect(() => {
    const sizeKey = `${stageSize.width}:${stageSize.height}`;
    if ((nodes.length > 0 || planEntities.length > 0) && lastFitSize.current !== sizeKey && stageSize.width > 100 && stageSize.height > 100) {
      lastFitSize.current = sizeKey;
      fitView();
    }
  }, [nodes.length, planEntities.length, stageSize, fitView]);

  // Coordinate transformations
  const worldToScreen = useCallback((p: Point): Point => {
    return [pan[0] + p[0] * zoom, pan[1] - p[1] * zoom];
  }, [pan, zoom]);

  const screenToWorld = useCallback((p: Point): Point => {
    return [(p[0] - pan[0]) / zoom, (pan[1] - p[1]) / zoom];
  }, [pan, zoom]);

  // Extract Wall Entities
  const walls = useMemo((): WallEntity[] => {
    const list: WallEntity[] = [];
    for (const node of nodes) {
      if (node["@type"] === "top:Wall") {
        const shape = (node["cad:geometry"] || {}) as Record<string, unknown>;
        const start = shape.start as Point | undefined;
        const end = shape.end as Point | undefined;
        if (Array.isArray(start) && Array.isArray(end) && start.length >= 2 && end.length >= 2) {
          const s: Point = [Number(start[0]), Number(start[1])];
          const e: Point = [Number(end[0]), Number(end[1])];
          const thickness = Number(shape.thickness || 200);
          list.push({
            id: node["@id"],
            node,
            start: s,
            end: e,
            thickness,
            length: dist(s, e),
            boundingRooms: Array.isArray(node["cad:boundingRooms"]) ? node["cad:boundingRooms"] : [],
          });
        }
      }
    }
    return list;
  }, [nodes]);

  // Map of wall junction points
  const junctions = useMemo(() => {
    const map = new Map<string, { point: Point; wallIds: Set<string> }>();
    const tolerance = 40;
    for (const w of walls) {
      for (const pt of [w.start, w.end]) {
        let matchedKey: string | null = null;
        for (const [key, entry] of map.entries()) {
          if (dist(pt, entry.point) <= tolerance) {
            matchedKey = key;
            break;
          }
        }
        if (matchedKey) {
          map.get(matchedKey)!.wallIds.add(w.id);
        } else {
          const key = `${Math.round(pt[0])},${Math.round(pt[1])}`;
          map.set(key, { point: pt, wallIds: new Set([w.id]) });
        }
      }
    }
    return map;
  }, [walls]);

  // Extract Space Centroids & Zones
  const spaceCenters = useMemo(() => {
    const map = new Map<string, { point: Point; label: string; area: number; zone: string; isEntry: boolean; node: GraphNode }>();
    for (const node of nodes) {
      if (SPACE_TYPES.has(node["@type"])) {
        const shape = (node["cad:geometry"] || {}) as Record<string, unknown>;
        const boundary = parsePoints(shape.boundary);
        if (boundary.length >= 3) {
          const cx = boundary.reduce((sum, p) => sum + p[0], 0) / boundary.length;
          const cy = boundary.reduce((sum, p) => sum + p[1], 0) / boundary.length;
          const label = node["rdfs:label"] || node["@id"].split(":").pop() || "Room";
          const area = typeof node["cad:areaSquareMetres"] === "number" ? node["cad:areaSquareMetres"] : 0;
          const zone = inferRoomZone(label, node["cad:properties"]);
          const isEntry = Boolean(node["cad:properties"]?.is_entry);
          map.set(node["@id"], { point: [cx, cy], label, area, zone, isEntry, node });
        }
      }
    }
    return map;
  }, [nodes]);

  // Stage Wheel Zoom
  const handleWheel = (e: KonvaEventObject<WheelEvent>) => {
    e.evt.preventDefault();
    const stage = stageRef.current;
    if (!stage) return;
    const pointer = stage.getPointerPosition();
    if (!pointer) return;

    const zoomFactor = e.evt.deltaY < 0 ? 1.15 : 0.87;
    const newZoom = Math.max(0.0001, Math.min(100, zoom * zoomFactor));

    const worldPoint: Point = [(pointer.x - pan[0]) / zoom, (pan[1] - pointer.y) / zoom];
    const newPan: Point = [
      pointer.x - worldPoint[0] * newZoom,
      pointer.y + worldPoint[1] * newZoom,
    ];

    setZoom(newZoom);
    setPan(newPan);
  };

  // Stage Pan Dragging & Measurement tool handling
  const [isPanning, setIsPanning] = useState(false);
  const panStartRef = useRef<{ pointer: Point; pan: Point } | null>(null);

  const handleStageMouseDown = (e: KonvaEventObject<MouseEvent>) => {
    const stage = stageRef.current;
    if (!stage) return;
    const pointer = stage.getPointerPosition();
    const world = pointer ? screenToWorld([pointer.x, pointer.y]) : [0, 0];

    // Measurement tool
    if (activeTool === "measure" && e.evt.button === 0) {
      if (!measureStart) {
        setMeasureStart(world as Point);
        setMeasureCurrent(world as Point);
        setMeasureResult(null);
      } else {
        const end = world as Point;
        const lengthM = dist(measureStart, end) / 1000;
        setMeasureResult({ start: measureStart, end, lengthM });
        setMeasureStart(null);
        setMeasureCurrent(null);
      }
      return;
    }

    // Middle click, or click on empty background for panning
    if (e.evt.button === 1 || e.target === stageRef.current) {
      setIsPanning(true);
      panStartRef.current = {
        pointer: [e.evt.clientX, e.evt.clientY],
        pan: [...pan] as Point,
      };
      if (e.target === stageRef.current && e.evt.button === 0) {
        onSelect(null);
        if (activeTool === "measure") {
          setMeasureStart(null);
          setMeasureResult(null);
        }
      }
    }
  };

  const handleStageMouseMove = (e: KonvaEventObject<MouseEvent>) => {
    const stage = stageRef.current;
    if (!stage) return;
    const pointer = stage.getPointerPosition();
    if (pointer) {
      const world = screenToWorld([pointer.x, pointer.y]);
      setCursorWorld([Math.round(world[0]), Math.round(world[1])]);

      if (activeTool === "measure" && measureStart) {
        setMeasureCurrent(world as Point);
      }
    }

    if (isPanning && panStartRef.current) {
      const dx = e.evt.clientX - panStartRef.current.pointer[0];
      const dy = e.evt.clientY - panStartRef.current.pointer[1];
      setPan([panStartRef.current.pan[0] + dx, panStartRef.current.pan[1] + dy]);
    }
  };

  const handleStageMouseUp = () => {
    setIsPanning(false);
    panStartRef.current = null;
  };

  // Snapping logic for point dragging
  const applySnapping = useCallback(
    (rawWorldPoint: Point, originPoint?: Point): Point => {
      let p: Point = [rawWorldPoint[0], rawWorldPoint[1]];

      if ((orthoMode || false) && originPoint) {
        const dx = Math.abs(p[0] - originPoint[0]);
        const dy = Math.abs(p[1] - originPoint[1]);
        if (dx > dy) p = [p[0], originPoint[1]];
        else p = [originPoint[0], p[1]];
      }

      if (snapJunction) {
        const snapToleranceMm = 200;
        let closestJunction: Point | null = null;
        let minDist = snapToleranceMm;
        for (const entry of junctions.values()) {
          const d = dist(p, entry.point);
          if (d < minDist) {
            minDist = d;
            closestJunction = entry.point;
          }
        }
        if (closestJunction) return closestJunction;
      }

      if (snapGrid && gridSizeMm > 0) {
        p = [
          Math.round(p[0] / gridSizeMm) * gridSizeMm,
          Math.round(p[1] / gridSizeMm) * gridSizeMm,
        ];
      }

      return p;
    },
    [gridSizeMm, junctions, orthoMode, snapGrid, snapJunction],
  );

  // Handle Wall Grip Drag End -> Emit changes
  const handleGripDragEnd = (wall: WallEntity, endpoint: "start" | "end", finalWorldPos: Point) => {
    if (!onCommitAction || !advancedGeometry) return;

    const oldPos = endpoint === "start" ? wall.start : wall.end;
    const otherPos = endpoint === "start" ? wall.end : wall.start;
    const snapped = applySnapping(finalWorldPos, otherPos);

    if (dist(oldPos, snapped) < 1) return;

    const connectedWallIds = new Set<string>([wall.id]);
    for (const w of walls) {
      if (dist(w.start, oldPos) <= 40 || dist(w.end, oldPos) <= 40) {
        connectedWallIds.add(w.id);
      }
    }

    const commands: EditorCommand[] = [];
    const changes: TopologyChange[] = [];

    for (const wid of connectedWallIds) {
      const targetWall = walls.find((w) => w.id === wid);
      if (!targetWall) continue;

      let newStart = [...targetWall.start] as Point;
      let newEnd = [...targetWall.end] as Point;

      if (dist(targetWall.start, oldPos) <= 40) newStart = snapped;
      if (dist(targetWall.end, oldPos) <= 40) newEnd = snapped;

      const updatedNode: GraphNode = {
        ...targetWall.node,
        "cad:geometry": {
          ...targetWall.node["cad:geometry"],
          start: newStart,
          end: newEnd,
        },
      };

      commands.push({ op: "upsert_node", semantic_id: targetWall.id, node: updatedNode });
      changes.push({
        op: "update",
        "@id": targetWall.id,
        "@type": "top:Wall",
        geometry: { start: newStart, end: newEnd },
      });
    }

    onCommitAction({
      action_id: crypto.randomUUID(),
      description: `Move shared wall junction (${connectedWallIds.size} dependent wall${connectedWallIds.size === 1 ? "" : "s"})`,
      commands,
      topology_changes: changes,
    });
  };

  // Render Infinite CAD Grid Lines
  const gridElements = useMemo(() => {
    const lines: JSX.Element[] = [];
    let minorMm = 500;
    if (zoom < 0.02) minorMm = 2000;
    else if (zoom < 0.05) minorMm = 1000;
    else if (zoom > 0.15) minorMm = 200;
    const majorMm = minorMm * 5;

    const startWorld = screenToWorld([0, stageSize.height]);
    const endWorld = screenToWorld([stageSize.width, 0]);

    const minX = Math.floor(startWorld[0] / minorMm) * minorMm;
    const maxX = Math.ceil(endWorld[0] / minorMm) * minorMm;
    const minY = Math.floor(startWorld[1] / minorMm) * minorMm;
    const maxY = Math.ceil(endWorld[1] / minorMm) * minorMm;

    for (let x = minX; x <= maxX; x += minorMm) {
      const isMajor = x % majorMm === 0;
      const isOrigin = x === 0;
      const [screenX] = worldToScreen([x, 0]);
      lines.push(
        <Line
          key={`vx_${x}`}
          points={[screenX, 0, screenX, stageSize.height]}
          stroke={isOrigin ? "#3b82f6" : isMajor ? "#222c38" : "#18202b"}
          strokeWidth={isOrigin ? 1.5 : isMajor ? 1 : 0.5}
          listening={false}
        />,
      );
    }

    for (let y = minY; y <= maxY; y += minorMm) {
      const isMajor = y % majorMm === 0;
      const isOrigin = y === 0;
      const [, screenY] = worldToScreen([0, y]);
      lines.push(
        <Line
          key={`hy_${y}`}
          points={[0, screenY, stageSize.width, screenY]}
          stroke={isOrigin ? "#3b82f6" : isMajor ? "#222c38" : "#18202b"}
          strokeWidth={isOrigin ? 1.5 : isMajor ? 1 : 0.5}
          listening={false}
        />,
      );
    }

    return lines;
  }, [pan, zoom, stageSize, worldToScreen, screenToWorld]);

  const selectedWall = walls.find((w) => w.id === selectedId);

  return (
    <div className="canvas-shell plan-canvas-shell" ref={containerRef}>
      {/* Architectural Tool Palette */}
      <div className="plan-tools">
        {/* Tool Mode Buttons */}
        <button
          type="button"
          className={`plan-tool-toggle ${activeTool === "select" ? "active" : ""}`}
          title="Pointer / Selection (V)"
          onClick={() => setActiveTool("select")}
        >
          ↖ Pointer
        </button>
        <button
          type="button"
          className={`plan-tool-toggle ${activeTool === "measure" ? "active" : ""}`}
          title="Measurement Tape (M) · Click 2 points to measure"
          onClick={() => {
            setActiveTool(activeTool === "measure" ? "select" : "measure");
            setMeasureStart(null);
            setMeasureResult(null);
          }}
        >
          📐 Measure
        </button>

        <div className="plan-tool-sep" />

        {/* View Zoom Tools */}
        <button
          type="button"
          className="plan-tool"
          title="Zoom In (+)"
          onClick={() => setZoom((z) => Math.min(z * 1.25, 100))}
        >
          +
        </button>
        <button
          type="button"
          className="plan-tool"
          title="Zoom Out (−)"
          onClick={() => setZoom((z) => Math.max(z / 1.25, 0.0001))}
        >
          −
        </button>
        <button type="button" className="plan-tool" title="Fit to Extents" onClick={fitView}>
          ⤢
        </button>

        <div className="plan-tool-sep" />

        {/* Geometry controls remain absent until the architect opts in. */}
        {advancedGeometry && <>
        <button
          type="button"
          className={`plan-tool-toggle ${snapGrid ? "active" : ""}`}
          title="Snap to 100mm Grid"
          onClick={() => setSnapGrid((v) => !v)}
        >
          Grid
        </button>
        <button
          type="button"
          className={`plan-tool-toggle ${snapJunction ? "active" : ""}`}
          title="Snap to Wall Junctions"
          onClick={() => setSnapJunction((v) => !v)}
        >
          Junctions
        </button>
        <button
          type="button"
          className={`plan-tool-toggle ${orthoMode ? "active" : ""}`}
          title="Orthogonal Axis Lock"
          onClick={() => setOrthoMode((v) => !v)}
        >
          Ortho
        </button>
        </>}
        <button
          type="button"
          className={`plan-tool-toggle ${showDimensions ? "active" : ""}`}
          title="Toggle Wall Dimensions"
          onClick={() => setShowDimensions((v) => !v)}
        >
          Dimensions
        </button>
        <button
          type="button"
          className={`plan-tool-toggle ${showRoomTags ? "active" : ""}`}
          title="Toggle Room Area Tags"
          onClick={() => setShowRoomTags((v) => !v)}
        >
          Room Tags
        </button>
      </div>

      {/* Konva 2D Stage */}
      <Stage
        ref={stageRef}
        width={stageSize.width}
        height={stageSize.height}
        onWheel={handleWheel}
        onMouseDown={handleStageMouseDown}
        onMouseMove={handleStageMouseMove}
        onMouseUp={handleStageMouseUp}
        style={{ cursor: isPanning ? "grabbing" : activeTool === "measure" ? "crosshair" : "default" }}
      >
        {/* Layer 1: Infinite Grid */}
        <Layer>{gridElements}</Layer>

        {/* Read-only source-DWG underlay. Semantic geometry is rendered above it. */}
        <Layer opacity={mode === "design_study" ? 0.38 : 0.68} listening={false}>
          {planEntities.map((entity) => {
            const shape = entity.geometry || {};
            const layerName = entity.layer.toUpperCase();
            const stroke = layerName.includes("WALL")
              ? "#a7b7c8"
              : layerName.includes("DOOR")
              ? "#f59e66"
              : layerName.includes("WATER")
              ? "#38bdf8"
              : layerName.includes("PLANT") || layerName.includes("LAWN")
              ? "#4ade80"
              : "#718096";
            if (shape.kind === "line") {
              const start = parsePoints([shape.start])[0];
              const end = parsePoints([shape.end])[0];
              if (!start || !end) return null;
              return <Line key={`cad:${entity.handle}`} points={[...worldToScreen(start), ...worldToScreen(end)]} stroke={stroke} strokeWidth={1.15} />;
            }
            if (shape.kind === "polyline") {
              const vertices = parsePoints(shape.vertices);
              if (vertices.length < 2) return null;
              return <Line key={`cad:${entity.handle}`} points={vertices.flatMap(worldToScreen)} closed={Boolean(shape.closed)} stroke={stroke} strokeWidth={1.1} />;
            }
            if (shape.kind === "arc") {
              const center = parsePoints([shape.center])[0];
              const radius = Number(shape.radius || 0) * zoom;
              if (!center || radius <= 0) return null;
              const start = Number(shape.start_angle || 0);
              const end = Number(shape.end_angle || 0);
              const angle = ((end - start) % 360 + 360) % 360 || 360;
              const screen = worldToScreen(center);
              return <Arc key={`cad:${entity.handle}`} x={screen[0]} y={screen[1]} innerRadius={radius} outerRadius={radius} angle={angle} rotation={-end} stroke={stroke} strokeWidth={1.1} />;
            }
            if (shape.kind === "circle") {
              const center = parsePoints([shape.center])[0];
              const radius = Number(shape.radius || 0) * zoom;
              if (!center || radius <= 0) return null;
              const screen = worldToScreen(center);
              return <Circle key={`cad:${entity.handle}`} x={screen[0]} y={screen[1]} radius={radius} stroke={stroke} strokeWidth={1.1} />;
            }
            if (shape.kind === "dimension" && showDimensions) {
              const start = parsePoints([shape.start])[0];
              const end = parsePoints([shape.end])[0];
              const textPosition = parsePoints([shape.text_position])[0];
              if (!start || !end || !textPosition) return null;
              const length = dist(start, end);
              if (length <= 0) return null;
              const normal: Point = [-(end[1] - start[1]) / length, (end[0] - start[0]) / length];
              const offset = (textPosition[0] - start[0]) * normal[0] + (textPosition[1] - start[1]) * normal[1];
              const a: Point = [start[0] + offset * normal[0], start[1] + offset * normal[1]];
              const b: Point = [end[0] + offset * normal[0], end[1] + offset * normal[1]];
              const label = String(shape.text_override || Number(shape.measurement || length).toFixed(1));
              const screen = worldToScreen(textPosition);
              return <Group key={`cad:${entity.handle}`}>
                <Line points={[...worldToScreen(start), ...worldToScreen(a), ...worldToScreen(b), ...worldToScreen(end)]} stroke={stroke} strokeWidth={0.8} />
                <Text x={screen[0] - 40} y={screen[1] - 10} width={80} align="center" text={label} fontSize={9} fill={stroke} />
              </Group>;
            }
            if (shape.kind === "point" && typeof shape.text === "string") {
              const position = parsePoints([shape.position])[0];
              if (!position) return null;
              const screen = worldToScreen(position);
              return <Text key={`cad:${entity.handle}`} x={screen[0]} y={screen[1]} text={shape.text} fontSize={Math.max(7, Math.min(15, Number(shape.height || 150) * zoom))} fill={stroke} />;
            }
            return null;
          })}
        </Layer>

        {/* Layer 2: Rooms & Space Program Polygons */}
        <Layer>
          {nodes
            .filter((node) => SPACE_TYPES.has(node["@type"]))
            .map((node) => {
              const boundary = parsePoints(node["cad:geometry"]?.boundary);
              if (boundary.length < 3) return null;
              const isSelected = selectedId === node["@id"];
              const isFailed = failedIds.has(node["@id"]);
              const flatPoints = boundary.flatMap((pt) => worldToScreen(pt));
              const center = spaceCenters.get(node["@id"]);
              const zoneConfig = center ? ZONE_COLORS[center.zone as keyof typeof ZONE_COLORS] : ZONE_COLORS.unassigned;

              return (
                <Group key={node["@id"]} onClick={() => onSelect(node["@id"])}>
                  {/* Color-Coded Room Fill Polygon */}
                  <Line
                    points={flatPoints}
                    closed
                    fill={
                      isSelected
                        ? "rgba(59, 130, 246, 0.25)"
                        : isFailed
                        ? "rgba(239, 68, 68, 0.2)"
                        : zoneConfig?.fill || "rgba(100, 116, 139, 0.1)"
                    }
                    stroke={
                      isSelected
                        ? "#38bdf8"
                        : isFailed
                        ? "#ef4444"
                        : zoneConfig?.stroke || "#475569"
                    }
                    strokeWidth={isSelected ? 2.5 : 1.2}
                    dash={node["cad:managed"] === false ? [8, 4] : undefined}
                    hitStrokeWidth={12}
                  />

                  {/* Room Center Title & Area Tag */}
                  {showRoomTags && center && (
                    <Group x={worldToScreen(center.point)[0]} y={worldToScreen(center.point)[1]}>
                      <Rect
                        x={-52}
                        y={-17}
                        width={104}
                        height={34}
                        fill="#0f172ae6"
                        stroke={isSelected ? "#38bdf8" : zoneConfig?.stroke || "#334155"}
                        strokeWidth={1}
                        cornerRadius={5}
                        shadowColor="#000"
                        shadowBlur={6}
                        shadowOpacity={0.5}
                      />
                      <Text
                        x={-50}
                        y={-12}
                        width={100}
                        text={center.label}
                        fontSize={10}
                        fontFamily="Inter, system-ui, sans-serif"
                        fontStyle="bold"
                        fill={isSelected ? "#bae6fd" : "#f1f5f9"}
                        align="center"
                        listening={false}
                      />
                      <Text
                        x={-50}
                        y={2}
                        width={100}
                        text={center.area > 0 ? `${center.area.toFixed(1)} m²` : zoneConfig?.label.split(" ")[0] || "Room"}
                        fontSize={8.5}
                        fontFamily="JetBrains Mono, monospace"
                        fill={zoneConfig?.text ? "#cbd5e1" : "#94a3b8"}
                        align="center"
                        listening={false}
                      />
                    </Group>
                  )}
                </Group>
              );
            })}
        </Layer>

        {/* Layer 3: Physical Walls & Dimension Lines */}
        <Layer>
          {walls.map((wall) => {
            const isSelected = selectedId === wall.id;
            const isFailed = failedIds.has(wall.id);
            const a = worldToScreen(wall.start);
            const b = worldToScreen(wall.end);
            const pixelThickness = Math.max(3, wall.thickness * zoom);
            const lengthM = (wall.length / 1000).toFixed(2);

            return (
              <Group key={wall.id} onClick={() => onSelect(wall.id)}>
                {/* Wall Solid Body */}
                <Line
                  points={[...a, ...b]}
                  stroke={isSelected ? "#facc15" : isFailed ? "#ef4444" : "#64748b"}
                  strokeWidth={pixelThickness}
                  lineCap="square"
                  hitStrokeWidth={Math.max(16, pixelThickness + 8)}
                />

                {/* Wall Centerline */}
                <Line
                  points={[...a, ...b]}
                  stroke={isSelected ? "#ca8a04" : "#94a3b8"}
                  strokeWidth={1}
                  dash={[4, 4]}
                  listening={false}
                />

                {/* Meter Wall Dimension Callout */}
                {showDimensions && (isSelected || zoom > 0.04) && (
                  <Group
                    x={(a[0] + b[0]) / 2}
                    y={(a[1] + b[1]) / 2}
                    rotation={(Math.atan2(b[1] - a[1], b[0] - a[0]) * 180) / Math.PI}
                  >
                    <Rect
                      x={-28}
                      y={-14}
                      width={56}
                      height={13}
                      fill="#0f172add"
                      cornerRadius={2}
                      stroke="#334155"
                      strokeWidth={0.5}
                      listening={false}
                    />
                    <Text
                      x={-28}
                      y={-12}
                      width={56}
                      text={`${lengthM} m`}
                      fontSize={8.5}
                      fontFamily="JetBrains Mono, monospace"
                      fontStyle="bold"
                      fill={isSelected ? "#fef08a" : "#e2e8f0"}
                      align="center"
                      listening={false}
                    />
                  </Group>
                )}
              </Group>
            );
          })}
        </Layer>

        {/* Layer 4: Doors & Swings */}
        <Layer>
          {nodes
            .filter((node) => PORTAL_TYPES.has(node["@type"]))
            .map((node) => {
              const shape = (node["cad:geometry"] || {}) as Record<string, unknown>;
              const pos = shape.position as Point | undefined;
              const isDoor = node["@type"] === "top:Door";
              const isSelected = selectedId === node["@id"];

              if (!Array.isArray(pos) || pos.length < 2) return null;
              const screenPos = worldToScreen([Number(pos[0]), Number(pos[1])]);
              const widthMm = Number(shape.width || 900);
              const radiusPx = widthMm * zoom;

              return (
                <Group key={node["@id"]} x={screenPos[0]} y={screenPos[1]} onClick={() => onSelect(node["@id"])}>
                  {/* 90-degree Architectural Door Swing Arc */}
                  {isDoor && radiusPx > 4 && (
                    <Arc
                      angle={90}
                      innerRadius={0}
                      outerRadius={radiusPx}
                      stroke={isSelected ? "#f97316" : "#ea580c"}
                      strokeWidth={1.5}
                      fill="rgba(234, 88, 12, 0.15)"
                      rotation={Number(shape.rotation || 0)}
                      listening={false}
                    />
                  )}
                  {/* Door Opening Marker */}
                  <Circle
                    radius={Math.max(4, 450 * zoom)}
                    fill={isSelected ? "#fdba74" : isDoor ? "#ea580c" : "#84cc16"}
                    stroke="#ffffff"
                    strokeWidth={1.2}
                  />
                </Group>
              );
            })}
        </Layer>

        {/* Layer 5: Circulation Pathways */}
        <Layer>
          {nodes
            .filter((node) => node["@type"] === "top:Connection")
            .map((node) => {
              const shape = (node["cad:geometry"] || {}) as Record<string, unknown>;
              if (shape.status === "rejected") return null;

              const fromCenter = spaceCenters.get(String(shape.from_space_id || ""));
              const toCenter = spaceCenters.get(String(shape.to_space_id || ""));
              if (!fromCenter || !toCenter) return null;

              const a = worldToScreen(fromCenter.point);
              const b = worldToScreen(toCenter.point);
              const isSelected = selectedId === node["@id"];
              const isFailed = failedIds.has(node["@id"]);
              const isCandidate = shape.status === "candidate";

              return (
                <Group key={node["@id"]} onClick={() => onSelect(node["@id"])}>
                  <Line
                    points={[...a, ...b]}
                    stroke={
                      isFailed
                        ? "#ef4444"
                        : isSelected
                        ? "#fde047"
                        : isCandidate
                        ? "#f59e0b"
                        : "#38bdf8"
                    }
                    strokeWidth={isSelected ? 4 : 2.5}
                    dash={isCandidate ? [8, 5] : undefined}
                    lineCap="round"
                    hitStrokeWidth={16}
                  />
                  <Circle
                    x={(a[0] + b[0]) / 2}
                    y={(a[1] + b[1]) / 2}
                    radius={isSelected ? 5 : 3.5}
                    fill={isCandidate ? "#f59e0b" : "#38bdf8"}
                    stroke="#0f172a"
                    strokeWidth={1.5}
                  />
                </Group>
              );
            })}
        </Layer>

        {/* Layer 6: Live Measurement Tape Tool */}
        {(measureStart || measureResult) && (
          <Layer>
            {measureStart && measureCurrent && (
              <Group>
                <Line
                  points={[...worldToScreen(measureStart), ...worldToScreen(measureCurrent)]}
                  stroke="#38bdf8"
                  strokeWidth={2}
                  dash={[6, 3]}
                />
                <Circle x={worldToScreen(measureStart)[0]} y={worldToScreen(measureStart)[1]} radius={4} fill="#38bdf8" />
                <Circle x={worldToScreen(measureCurrent)[0]} y={worldToScreen(measureCurrent)[1]} radius={4} fill="#38bdf8" />
                <Group
                  x={(worldToScreen(measureStart)[0] + worldToScreen(measureCurrent)[0]) / 2}
                  y={(worldToScreen(measureStart)[1] + worldToScreen(measureCurrent)[1]) / 2 - 12}
                >
                  <Rect x={-36} y={-10} width={72} height={20} fill="#0284c7" cornerRadius={4} />
                  <Text
                    x={-36}
                    y={-5}
                    width={72}
                    text={`${(dist(measureStart, measureCurrent) / 1000).toFixed(2)} m`}
                    fontSize={10}
                    fontFamily="JetBrains Mono, monospace"
                    fontStyle="bold"
                    fill="#ffffff"
                    align="center"
                  />
                </Group>
              </Group>
            )}

            {measureResult && (
              <Group>
                <Line
                  points={[...worldToScreen(measureResult.start), ...worldToScreen(measureResult.end)]}
                  stroke="#4ade80"
                  strokeWidth={2}
                />
                <Circle x={worldToScreen(measureResult.start)[0]} y={worldToScreen(measureResult.start)[1]} radius={4} fill="#4ade80" />
                <Circle x={worldToScreen(measureResult.end)[0]} y={worldToScreen(measureResult.end)[1]} radius={4} fill="#4ade80" />
                <Group
                  x={(worldToScreen(measureResult.start)[0] + worldToScreen(measureResult.end)[0]) / 2}
                  y={(worldToScreen(measureResult.start)[1] + worldToScreen(measureResult.end)[1]) / 2 - 12}
                >
                  <Rect x={-40} y={-10} width={80} height={20} fill="#166534" cornerRadius={4} />
                  <Text
                    x={-40}
                    y={-5}
                    width={80}
                    text={`${measureResult.lengthM.toFixed(2)} m (${Math.round(measureResult.lengthM * 1000)}mm)`}
                    fontSize={9.5}
                    fontFamily="JetBrains Mono, monospace"
                    fontStyle="bold"
                    fill="#ffffff"
                    align="center"
                  />
                </Group>
              </Group>
            )}
          </Layer>
        )}

        {/* Layer 7: Interactive Wall Junction Grips */}
        {mode === "design_study" && advancedGeometry && selectedWall && (
          <Layer>
            {/* Start Grip Handle */}
            <Group
              x={worldToScreen(selectedWall.start)[0]}
              y={worldToScreen(selectedWall.start)[1]}
              draggable
              onDragMove={(e) => {
                const stage = stageRef.current;
                if (!stage) return;
                const pos = screenToWorld([e.target.x(), e.target.y()]);
                const snapped = applySnapping(pos, selectedWall.end);
                setCursorWorld([Math.round(snapped[0]), Math.round(snapped[1])]);
              }}
              onDragEnd={(e) => {
                const finalPos = screenToWorld([e.target.x(), e.target.y()]);
                handleGripDragEnd(selectedWall, "start", finalPos);
              }}
            >
              <Circle radius={8} fill="#3b82f6" stroke="#ffffff" strokeWidth={2} shadowColor="#000" shadowBlur={6} />
              <Text x={11} y={-5} text="Junction A" fontSize={9} fontFamily="JetBrains Mono, monospace" fill="#93c5fd" />
            </Group>

            {/* End Grip Handle */}
            <Group
              x={worldToScreen(selectedWall.end)[0]}
              y={worldToScreen(selectedWall.end)[1]}
              draggable
              onDragMove={(e) => {
                const stage = stageRef.current;
                if (!stage) return;
                const pos = screenToWorld([e.target.x(), e.target.y()]);
                const snapped = applySnapping(pos, selectedWall.start);
                setCursorWorld([Math.round(snapped[0]), Math.round(snapped[1])]);
              }}
              onDragEnd={(e) => {
                const finalPos = screenToWorld([e.target.x(), e.target.y()]);
                handleGripDragEnd(selectedWall, "end", finalPos);
              }}
            >
              <Circle radius={8} fill="#3b82f6" stroke="#ffffff" strokeWidth={2} shadowColor="#000" shadowBlur={6} />
              <Text x={11} y={-5} text="Junction B" fontSize={9} fontFamily="JetBrains Mono, monospace" fill="#93c5fd" />
            </Group>
          </Layer>
        )}
      </Stage>

      {/* Bottom CAD Status Bar */}
      <div className="plan-status-bar">
        <div className="status-segment">
          <span className="status-label">Cursor:</span>
          <span className="status-value">X: {(cursorWorld[0] / 1000).toFixed(2)}m</span>
          <span className="status-value">Y: {(cursorWorld[1] / 1000).toFixed(2)}m</span>
        </div>
        <div className="status-segment">
          <span className="status-label">Scale:</span>
          <span className="status-value">{(zoom * 1000).toFixed(0)} px/m</span>
        </div>
        <div className="status-segment">
          <span className="status-label">Tool:</span>
          <span className="status-value uppercase">{activeTool}</span>
        </div>
        <div className="status-segment right">
          <span>{mode === "design_study" ? "Interactive Drafting Active" : "Floor Plan View"}</span>
        </div>
      </div>
    </div>
  );
}
