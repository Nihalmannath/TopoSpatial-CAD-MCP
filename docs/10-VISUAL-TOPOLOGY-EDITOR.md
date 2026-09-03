# Interactive topology playground

## Purpose

The studio exposes architectural topology when a language model does not have
enough project-specific planning knowledge. Live mode is read-only. Playground
mode provides one isolated draft for architectural programs, desired spatial
relationships, actual circulation, portal association, routing, and clearance.
It is not a second CAD database: the DWG remains authoritative and changes
require Preview then Apply.

The backend merges both layers into one graph:

```text
DWG snapshot + XData ──> cached topology analysis ──┐
                                                    ├─> merged committed graph
workspace semantic sidecar ────────────────────────┘
                                                          │
                               visual draft (isolated) <──┘
```

An LLM receives the committed graph plus compact draft/conflict status. It does
not receive unapproved draft contents unless it explicitly requests
`manage_design` action `get_draft_context`.

## Build and run

```powershell
cd frontend
npm install
npm run build
cd ..
python src/server.py
```

Open `http://127.0.0.1:8888/editor`. The dashboard binds to loopback by default
and issues a process-local session token for mutating editor requests.

## Semantic model

- `top:Room` identifies an explicitly classified architectural room.
- `top:Space` represents any explicit usable or circulation polygon, including
  corridors, lobbies, stairs, terraces, or external entry space.
- `top:Opening` is a traversable hosted opening without a door leaf.
- `top:SpatialIntent` records what should, should not, or preferably should be
  true between spaces. It is sidecar-only and never creates CAD geometry.
- `top:Connection` is a directional or bidirectional circulation edge between
  two rooms/spaces, optionally through a door or opening.

In the default graph, a confirmed `top:Door` or `top:Opening` is rendered on its
`top:Connection` edge rather than as an extra intermediate node. Select the edge
to inspect the portal mark, clear width, host wall, CAD handle, and connected
spaces. Geometric adjacency remains a separate dotted relation and never proves
that a doorway exists.

## Existing drawings and portal discoveries

The editor detects native AEC portals and recognised block references without
silently classifying them. A discovery explains its source handles, evidence,
candidate endpoints, and any ambiguity. `Review & Confirm` stages XData on the
existing CAD object through Preview/Apply; Save to Design Study and Preview do
not mutate the DWG.
- Physical walls remain one shared network. A wall can have multiple
  `cad:boundingRooms`; moving its endpoint batches every wall at that junction
  and updates each affected semantic boundary.

Arbitrary valid 2D polygons are supported. Shapely performs deterministic
polygon validation, geometry comparisons, and clearance buffering. TopologicPy
continues to provide architectural faces, area computation, and semantic
topological reasoning when the optional topology dependency is enabled.

## Architect workflow

1. Enter Playground to pin the drawing and graph revisions.
2. Select spaces in the synchronized plan and graph views.
   Untagged enclosed areas remain candidates until you explicitly promote them
   to spaces in the sandbox.
3. Edit the selected space's type, zone, intended use, area range, occupancy,
   privacy, access, environment, acoustic requirement, and notes. One form save
   is one undoable architect action; values are not emitted per keystroke.
4. Add `top:SpatialIntent` relationships such as must direct-access, should be
   near, or must-not direct-access. Add a `top:Connection` only for a real path.
5. Save to Sandbox, then Send to MCP. The server stores an immutable,
   revision-bound inbox request and produces a compact prompt/JSON package.
6. Use local undo/redo (up to 100 actions) or Reset Draft.
7. Create a formal preview, review semantic/CAD diffs, acknowledge advisory
   diagnostics, and Apply explicitly.

The default configured diagnostic width is 1200 mm. Diagnostics are design aids,
not building-code certification. They report missing entrances, unreachable
spaces, missing portal/space references, declared widths below the configured
minimum, and polygon-clearance disconnection where sufficient geometry exists.

## Revisions, drafts, and conflicts

Schema-version-3 workspaces store up to 100 architect actions rather than an
ambiguous command list. A version-2 dirty draft is preserved as one
`Imported version-2 draft` action; committed overrides, deletions, conflicts,
last-common graph, and revisions are retained. Empty drafts are not dirty.
Draft evaluation and draft-aware routing use cached graph data only.

MCP inbox requests are durable workspace records. Re-sending an unchanged draft
returns the same ID; editing supersedes the old immutable request. Agents use
`list_editor_requests`, `get_editor_request`, and `preview_editor_request` so
they consume authoritative stored actions without reconstructing them from chat.

Every formal preview is bound to both the SHA-256 drawing revision and semantic
graph revision and expires after the configured transaction TTL. Preview does not
modify CAD, committed workspace state, or graph exports. Cancel removes only the
formal preview and preserves the playground. Apply performs CAD
changes first, commits semantic state, refreshes analysis, and atomically writes
JSON-LD/Turtle sidecars. A failed semantic commit requests CAD undo.

When CAD geometry and an editor-owned geometry field both change from their last
common state, the editor records a split-authority conflict. The user must choose
the CAD or editor value; it is never silently overwritten.

## AutoCAD palette and events

`plugins/autocad` contains separate projects because Autodesk changed runtime:

- AutoCAD/AutoCAD Architecture 2024: .NET Framework 4.8.
- AutoCAD/AutoCAD Architecture 2025: .NET 8 for Windows.

Build against the matching AutoCAD managed assemblies, load the DLL with
`NETLOAD`, and run `TOPOSTUDIO`. The plugin embeds the same local editor in a
WebView2 palette. Database/document event handlers only collect changed handles;
they do not mutate CAD or open transactions. Notifications are coalesced and
sent after command completion/idle only after the user enables them with
`TOPOSTUDIOEVENTS`. By default the studio performs one initial read and refreshes
only when the user clicks Refresh. WebView focus/select messages are typed,
origin checked, and bound to the expected drawing; the plugin never creates,
activates, or switches a document for a palette message.

## Current scope

Version 1 is local-only and supports 2D straight-wall plans with arbitrary room
and space polygons. It does not provide automated code compliance, obstacle-level
path planning, 3D solids, IFC editing, or automatic semantic classification.
