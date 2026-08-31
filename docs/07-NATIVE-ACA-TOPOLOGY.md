# Native AutoCAD Architecture and Topology Workflow

This guide describes the supported production workflow for creating and
maintaining semantic 2D architectural objects through `manage_topology`. It also
explains how native AutoCAD Architecture (ACA) objects differ from generic CAD
linework.

## What the Integration Creates

When ACA is active and native authoring is selected, the MCP creates:

| Semantic object | CAD representation | Default layer |
|---|---|---|
| Room | Closed non-plot `AcDb2dPolyline` boundary | `AI-ROOMS` |
| Wall | Native `AecDbWall` | `AI-WALLS` |
| Door | Native `AecDbDoor` anchored to a native wall | `AI-DOORS` |
| Window | Native `AecDbWindow` anchored to a native wall | `AI-WINDOWS` |

The room boundary is semantic metadata geometry, not a substitute for the four
walls. Every managed entity receives `TOPOSPATIAL_TOPOLOGY` XData containing its
semantic ID, ontology class, label, group ID, schema version, geometry contract,
managed flag, and resolved representation where applicable.

On ordinary AutoCAD, ZWCAD, GstarCAD, or BricsCAD, the same semantic operations
use portable polylines, lines, and arcs when `representation` is `auto` or
`standard`.

## Before Calling `manage_topology`

1. Install the topology extra:

   ```powershell
   uv sync --extra dev --extra topology
   ```

2. Completely restart the MCP client after installation or configuration
   changes.
3. Start the CAD product and open the target drawing.
4. Set the active drawing's `INSUNITS` to millimetres (`4`).
5. Check the live product and installed styles:

   ```json
   {
     "operations": "[{\"action\":\"status\"},{\"action\":\"capabilities\",\"include_styles\":true}]"
   }
   ```

For native ACA creation, the capability response must report:

```json
{
  "native_architecture": {
    "native_aec": true,
    "aec_api_version": "8.6",
    "supported_objects": ["wall", "door", "window"],
    "styles": {
      "wall": ["Standard"],
      "door": ["Standard"],
      "window": ["Standard"]
    }
  }
}
```

The AEC version shown above is only an example. The adapter discovers registered
AEC automation versions at runtime and is not tied to one ACA release.

## Representation Policy

Every create or update change supports one policy:

- `auto` is the default. It selects native ACA objects when available and uses
  standard geometry otherwise. A native opening also requires a native wall
  host.
- `native_aec` requires native ACA support. Preview fails if ACA is unavailable,
  a requested style does not exist, or a door/window host is not native.
- `standard` always creates portable CAD geometry.

The resolved representation is frozen during preview and stored in XData. Apply
does not make a different representation decision later.

## Complete Example: 5000 × 4000 mm Room

The requested dimensions are clear interior dimensions. With 200 mm walls, this
room has a 5400 × 4400 mm exterior footprint and a 20 m² clear area.

### 1. Analyze

Call:

```text
manage_topology(action="analyze", scope="all")
```

Copy the returned `revision`. Untagged enclosed regions appear only in
`candidates`; they are never silently classified as rooms.

### 2. Preview

Replace `sha256:CURRENT_DRAWING_REVISION` with the analysis revision:

```json
{
  "action": "preview",
  "scope": "all",
  "payload": {
    "@context": {
      "top": "http://w3id.org/topologicpy#",
      "cad": "urn:topospatial:cad#"
    },
    "base_revision": "sha256:CURRENT_DRAWING_REVISION",
    "changes": [
      {
        "op": "create",
        "@id": "urn:demo:room:studio",
        "@type": "top:Room",
        "label": "Native ACA Demo Room",
        "representation": "native_aec",
        "geometry": {
          "origin": [0, 0],
          "clear_width": 5000,
          "clear_depth": 4000,
          "wall_thickness": 200,
          "rotation_deg": 0,
          "wall_height": 3000,
          "wall_style": "Standard"
        }
      },
      {
        "op": "create",
        "@id": "urn:demo:door:entry",
        "@type": "top:Door",
        "label": "Entry Door",
        "representation": "native_aec",
        "geometry": {
          "host_wall_id": "urn:demo:room:studio:wall:1",
          "offset": 2250,
          "width": 900,
          "height": 2100,
          "style": "Standard",
          "hinge": "left",
          "swing": "in",
          "swing_angle_deg": 90
        }
      },
      {
        "op": "create",
        "@id": "urn:demo:window:east",
        "@type": "top:Window",
        "label": "East Window",
        "representation": "native_aec",
        "geometry": {
          "host_wall_id": "urn:demo:room:studio:wall:2",
          "offset": 1400,
          "width": 1200,
          "height": 1200,
          "sill_height": 900,
          "style": "Standard"
        }
      },
      {
        "op": "create",
        "@id": "urn:demo:window:north",
        "@type": "top:Window",
        "label": "North Window",
        "representation": "native_aec",
        "geometry": {
          "host_wall_id": "urn:demo:room:studio:wall:3",
          "offset": 2100,
          "width": 1200,
          "height": 1200,
          "sill_height": 900,
          "style": "Standard"
        }
      }
    ]
  }
}
```

The room automatically owns these wall IDs, in order:

- `:wall:1` — south wall
- `:wall:2` — east wall
- `:wall:3` — north wall
- `:wall:4` — west wall

Review `diff`, `warnings`, `affected handles`, and each resolved
`representation`. Preview does not modify the DWG (`"mutated": false`).

### 3. Apply

Use the returned transaction ID:

```json
{
  "action": "apply",
  "scope": "all",
  "payload": {"transaction_id": "PREVIEW_TRANSACTION_ID"}
}
```

Apply rechecks the active drawing name and SHA-256 revision. A transaction
expires after 600 seconds by default. Applying the same completed transaction
again is idempotent and returns the stored result without creating duplicates.

### 4. Verify and Export

```text
manage_topology(action="analyze", scope="all")
manage_topology(action="query", payload={"class": "top:Door"})
manage_topology(action="query", payload={"relation": "top:isPartOf"})
manage_topology(action="export", format="jsonld")
manage_topology(action="export", format="ttl")
```

For this example, a successful native result contains eight semantic nodes:
one room, four walls, one door, and two windows. Relations include
`top:containsElement`, `top:isPartOf`, opening `cad:hostWall` identifiers,
`top:adjacentTo`, and door-derived `top:connectsTo` where applicable.

Both sidecars are written atomically to `output.directory`:

```text
<drawing>.topology.jsonld
<drawing>.topology.ttl
```

Save the DWG separately with `manage_files` after the topology apply.

## Supported Change Operations

### Annotate existing geometry

`annotate` can tag explicit handles, one layer, or a detected candidate-room ID.
Existing room annotation requires one closed polyline; an enclosed set of legacy
lines should be annotated through its candidate ID. Classification remains an
explicit user/agent decision.

### Create

- Room: origin, clear width/depth, wall thickness, rotation, height, wall style
- Wall: centerline start/end, thickness, height, style
- Door: host wall ID, offset from host start, width, height, style, hinge, swing
- Window: host wall ID, offset from host start, width, height, sill, style

All coordinates and distances use native drawing millimetres.

### Update

Only MCP-managed geometry can be replaced. A managed room cannot be geometrically
replaced while its owned walls host doors or windows; update or remove dependent
openings first. Legacy annotated geometry allows label changes but not geometry
replacement.

### Delete

Deletion is limited to MCP-managed objects. The tool refuses to delete untagged
or legacy geometry. Deleting a wall or room with hosted openings requires
`"cascade": true`, and the preview lists everything that will be removed.

## Transaction and Thread Safety

- CAD COM objects remain on the calling worker thread. Geometry passed to the
  analysis worker is a plain immutable coordinate snapshot.
- Each MCP/dashboard worker resolves its own thread-local CAD adapter.
- Preview transactions are held in the running MCP server. Restarting the server
  invalidates pending transaction IDs; analyze and preview again.
- Apply runs inside one AutoCAD undo group. Sidecars are written only after CAD
  mutation and post-apply analysis succeed.
- If mutation or post-validation fails, the undo group is rolled back and the
  bridge waits until the original drawing revision is observable.

## Guidance for Claude and Other Agents

Use a prompt like this when native architectural objects are required:

> Check `manage_session` status and capabilities first. Use
> `manage_topology`, not `draw_entities`, for every room, wall, door, and window.
> Analyze the active millimetre drawing, preview all changes with
> `representation: native_aec`, show me the diff and warnings, and apply only the
> returned transaction. Then analyze again, save the DWG, and report native object
> types and sidecar paths.

If an agent uses `draw_entities`, it can create a visually similar plan made of
standard entities, but it will not create ACA wall/door/window objects or the
semantic topology graph.

## Version 1 Limits

- 2D architectural plans only
- Rectangular managed rooms and straight walls
- Single-swing doors and hosted windows
- No 3D solids, IFC import, routing, arbitrary curve topology, or automatic AI
  semantic classification
- Native ACA creation currently covers walls, doors, and windows; spaces, slabs,
  stairs, roofs, and schedules are not authored by `manage_topology`

