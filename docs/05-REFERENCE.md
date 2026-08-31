# 05 - Tool Reference

## Summary

**8 unified MCP tools**, including the optional topology workflow:

| Unified Tool | Actions | Category |
|--------------|---------|----------|
| `manage_session` | connect, disconnect, status, capabilities, zoom_extents, undo, redo, screenshot, export_view, check_running, open_dashboard, list_supported | Connection & Control |
| `draw_entities` | line, circle, arc, rect, polyline, spline, text, dimension, leader, mleader, table | Drawing |
| `manage_blocks` | list, info, insert, create, get_attrs, set_attrs | Blocks |
| `manage_layers` | create, delete, rename, on, off, set_color, is_on, list, info | Layers |
| `manage_files` | save, new, close, list, switch | Files |
| `manage_entities` | select, move, rotate, scale, set_color, set_layer, set_color_bylayer, copy, paste, delete | Entities |
| `export_data` | scope=all/selected, format=json/excel | Export |
| `manage_topology` | analyze, query, preview, apply, export | Architectural topology |

---

## Shorthand Format

All tools accept operations as a **plain-text shorthand** (one per line), which is far more token-efficient than JSON:

```
# draw_entities
line|0,0|100,0|red|walls
circle|50,40|10|blue
table|0,0|4|3|3|15|Precios|Item;Cant;Val|Acero;10;150~~Mano;5;120

# manage_layers
create|walls|red|50
off|Defpoints,notes

# manage_entities
select|layer|walls
move|A1B2,C3D4|10|5
```

JSON arrays are also accepted for backwards compatibility.

---

## manage_session

Connection lifecycle, capability discovery, view control, capture, and history.
Unlike the shorthand tools, `manage_session.operations` is a JSON object or
array encoded as a string.

```json
[
  {"action": "check_running"},
  {"action": "status"},
  {"action": "capabilities", "include_styles": true},
  {"action": "zoom_extents"},
  {"action": "screenshot"}
]
```

Supported actions:

- `connect` / `disconnect`
- `status`, `check_running`, `list_supported`
- `capabilities` with optional `include_styles`
- `zoom_extents`, `screenshot`, `export_view`
- `undo` / `redo` with optional `count`
- `open_dashboard` with optional `host` and `port`

`screenshot` captures the full CAD window, including UI chrome, through the
live application HWND. It works when another application obscures CAD and
handles high-DPI displays. `export_view` uses the product's command-based image
export and may not be supported by every CAD/version.

---

## draw_entities

Create geometric entities. Shorthand: `type|param1|param2|...|color|layer`

```
line|start|end|color|layer                    → line|0,0|10,10|red|walls
circle|center|radius|color                    → circle|5,5|3|blue
arc|center|radius|start_angle|end_angle       → arc|0,0|5|0|90
rect|corner1|corner2|color                    → rect|0,0|20,15
polyline|pts(;sep)|closed|color               → polyline|0,0;10,10;20,0|closed
spline|pts(;sep)|closed|color                 → spline|0,0;5,10;10,0
text|pos|text|height|color                    → text|5,5|Hello World|2.5
dimension|start|end|color                     → dimension|0,0|10,0
leader|pts(;sep)|text|height|color|layer      → leader|0,0;10,10|My note|2.5|red
leader|group1~~group2|text|...                → leader|0,0;10,10~~20,0;10,10|Label
table|ins|rows|cols|row_h|col_w|title|headers|data → table|0,0|4|3|3|15|Precios|Item;Cant;Val|Acero;10;150~~Mano;5;120
```

**DEFAULTS:** `color=white`, `layer=0`

**Leaders:** first point = arrowhead, last point = text attach. Use `~~` for multi-arrow.

**Tables (alias `tab`):** Row 0 is Title, Row 1 contains column Headers, Row 2 onwards are Data rows. Semicolon `;` separates column/cell values. Double tildes `~~` separate data rows.

---

## manage_layers

```
create|name|color|lineweight     → create|walls|red|50
delete|name                      → delete|temp
rename|old|new                   → rename|Layer1|furniture
on|names(,sep)                   → on|walls,doors
off|names(,sep)                  → off|Defpoints
set_color|name|color             → set_color|0|white
is_on|name                       → is_on|walls
list                             # list all layer names
info                             # full layer details (color, locked, frozen)
```

**DEFAULTS:** `color=white`, `lineweight=25`

---

## manage_blocks

```
list                                           # list all block definitions
info|block_name|include                        → info|Door|both
insert|name|point|scale|rotation|layer|color  → insert|Door|10,20|1.5|90|walls|red
create|name|handles|point|description         → create|MyBlock|A1,B2|0,0|Desc
get_attrs|handle                              → get_attrs|A1B2C3
set_attrs|handle|{"TAG": "value"}             → set_attrs|A1B2C3|{"POLOS": "4P"}
```

**`include`:** `info` (default) | `references` | `both`

---

## manage_entities

```
select|by|value               → select|layer|walls
move|handles|dx|dy            → move|A1,B2|10|5
rotate|handles|angle|cx|cy   → rotate|A1|45|0|0
scale|handles|factor|cx|cy   → scale|A1|2.0|0|0
set_color|handles|color      → set_color|A1,B2|red
set_layer|handles|layer      → set_layer|A1|walls
set_color_bylayer|handles    → set_color_bylayer|A1,B2
copy|handles                 → copy|A1,B2
paste|base_point             → paste|100,200
delete|handles               → delete|A1,B2
```

**`by`:** `color` | `layer` | `type`
**`handles`:** comma-separated entity handles (e.g. `A1B2,C3D4`)

---

## manage_files

```
save|path_or_filename|format  → save|/path/file.dwg
save|filename                 → save|backup.dwg
new                           # create new drawing
close|save_changes            → close|true
list                          # list open drawings
switch|drawing_name           → switch|floor_plan.dwg
```

**`format`:** `dwg` (default) | `dxf` | `pdf`

---

## export_data

Extract entity data as JSON or export to Excel (3 sheets: Entities, Layers, Blocks).

```
scope=all, format=json              # all entities as JSON
scope=all, format=excel             # export to [drawing_name]_data.xlsx
scope=selected, format=json         # selected entities as JSON
scope=selected, format=excel, filename=selection.xlsx
```

**Excel columns:** `Handle`, `ObjectType`, `Layer`, `Color`, `Length`, `Area`, `Radius`, `Circumference`, `Name`

---

## manage_topology

2D architectural semantics for rooms, straight walls, single-swing doors, and
windows. Install the supported topology backend with
`uv sync --extra dev --extra topology`.
Classification is always explicit: untagged closed regions are candidates, not
semantic rooms.

```text
manage_topology(action="analyze", scope="all")
manage_topology(action="query", payload={"class": "top:Room"})
manage_topology(action="export", format="ttl")
```

Mutations require two calls. First pass the current analysis revision in a strict
change document; preview returns an expiring transaction without changing CAD:

```json
{
  "action": "preview",
  "payload": {
    "@context": {"top": "http://w3id.org/topologicpy#"},
    "base_revision": "sha256:...",
    "changes": [
      {
        "op": "create",
        "@id": "urn:room:living",
        "@type": "top:Room",
        "label": "Living Room",
        "geometry": {
          "origin": [0, 0],
          "clear_width": 5000,
          "clear_depth": 4000,
          "wall_thickness": 200,
          "rotation_deg": 0,
          "wall_height": 3000,
          "wall_style": "Standard"
        }
      }
    ]
  }
}
```

Apply the returned transaction only after reviewing its diff:

```text
manage_topology(action="apply", payload={"transaction_id": "..."})
```

Each create/update change can set:

```text
representation = auto | native_aec | standard
```

`auto` selects native ACA walls/openings when possible and otherwise uses
portable standard entities. `native_aec` makes missing ACA support, styles, or
native opening hosts a preview error. The resolved representation is included in
the diff and persisted in XData.

Preview transactions expire after 10 minutes by default and are rejected if the
drawing revision changes. Preview/apply require the drawing's `INSUNITS` to be
millimetres. Generated room dimensions describe the clear interior;
the example above has a 5400 × 4400 mm exterior footprint. `AI-ROOMS` boundaries
are non-plot. Complete `<drawing>.topology.jsonld` and
`<drawing>.topology.ttl` sidecars are written to the configured export directory
after apply or export.

Supported change contracts:

| Class | Required geometry | Optional/default geometry |
|---|---|---|
| `top:Room` | `origin`, `clear_width`, `clear_depth` | `wall_thickness=200`, `rotation_deg=0`, `wall_height=3000`, `wall_style=Standard` |
| `top:Wall` | `start`, `end` | `thickness=200`, `height=3000`, `style=Standard` |
| `top:Door` | `host_wall_id`, `hinge`, `swing` | `offset=0`, `width=900`, `height=2100`, `style=Standard`, `swing_angle_deg=90` |
| `top:Window` | `host_wall_id` | `offset=0`, `width=1200`, `height=1200`, `sill_height=900`, `style=Standard` |

`annotate` targets handles, a layer, or a candidate ID. `update` and `delete`
geometry operations are restricted to MCP-managed objects. Deleting hosted
objects requires an explicit cascade. See
[Native AutoCAD Architecture and Topology Workflow](07-NATIVE-ACA-TOPOLOGY.md)
for a complete room/door/window transaction.

---

## Common Parameters

### Coordinates
```
"0,0"        # 2D
"0,0,0"      # 3D
"100.5,-20"  # Floats and negatives
```

### Colors
`red`, `blue`, `green`, `yellow`, `cyan`, `magenta`, `white`, `black`, `gray`, `orange`

### CAD Types
`autocad`, `zwcad`, `gcad`, `bricscad`

### Entity Types (for select|type|...)
`LINE`, `CIRCLE`, `ARC`, `LWPOLYLINE`, `POLYLINE`, `TEXT`, `MTEXT`, `INSERT`, `DIMENSION`, `SPLINE`, `POINT`, `HATCH`
