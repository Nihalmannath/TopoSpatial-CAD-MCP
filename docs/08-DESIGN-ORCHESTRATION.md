# 08 - Efficient Design Orchestration

## Purpose

`manage_design` is the preferred high-level MCP tool for architectural work. It
keeps design intent with the controlling model while moving deterministic
geometry, topology, dependency ordering, validation, batching, retries, and
transaction handling into the local server.

```text
LLM design intent
        ↓
typed manage_design request
        ↓
DesignOrchestrator
├── capture plain CAD snapshot
├── reuse revision-cached topology
├── resolve bounded semantic context
├── compile one ExecutionPlan
├── order hosts before openings
├── validate geometry and dependencies
├── create non-mutating preview
└── return compact summary
        ↓ approval
one CAD undo-group apply
        ↓
post-apply validation + sidecars
```

The low-level tools remain available for debugging, decorative geometry, and
unsupported cases. They are not the default for room/wall/opening changes.

## Mandatory modification rule

For a modification request, never create a new drawing unless the user
explicitly requests one. Modify the active drawing in place through a
preview/apply transaction.

Before calling low-level drawing tools:

1. Identify the affected semantic entities.
2. Request only their bounded topology neighborhood.
3. Create one local execution plan.
4. Batch dependent CAD mutations.
5. Validate locally.
6. Return a compact summary.

Do not call the LLM between deterministic geometry operations. Do not rebuild
topology when the exact drawing revision is cached. Capture images only at
meaningful checkpoints, preferably once after preview and once after final
apply. Stop after preview whenever approval is required.

These rules are also recorded in the repository-level `AGENTS.md` file.

## Typed actions

`manage_design` accepts one `request` object discriminated by `action`:

| Action | Class | Mutates CAD | Purpose |
|---|---|---:|---|
| `inspect` | READ | No | Compact drawing, semantic, candidate, and issue counts |
| `get_context` | READ | No | Bounded semantic neighborhood by exact ID or label |
| `get_result` | READ | No | Page a truncated context result without CAD access |
| `create` | PLAN | No | Preview a batch containing only `create` changes |
| `modify` | PLAN | No | Preview a mixed annotate/create/update/delete batch |
| `validate` | PLAN | No | Build and validate a plan without storing a transaction |
| `preview` | PLAN | No | Preview a general mixed change batch |
| `apply` | WRITE | Yes | Apply one unexpired transaction atomically |
| `cancel` | WRITE control | No | Cancel a pending transaction |
| `rollback` | WRITE control | Yes | Undo the last applied transaction when still safe |
| `metrics` | READ | No | Read task proxy metrics; model tokens remain unknown |

Every request rejects unknown fields. The schema exposes real arrays, objects,
booleans, enums, and nullability to MCP clients; callers do not need to encode
structured values as strings.

## Recommended create workflow

### 1. Inspect

```json
{
  "request": {
    "action": "inspect",
    "task_id": "create-2bhk",
    "detail_level": "summary"
  }
}
```

Keep the returned `revision`.

### 2. Submit one architectural batch

This shortened example creates a wall and a hosted door. A complete plan can
submit all approved walls, doors, and windows in the same `changes` array.

```json
{
  "request": {
    "action": "create",
    "task_id": "create-2bhk",
    "base_revision": "sha256:...",
    "detail_level": "normal",
    "changes": [
      {
        "op": "create",
        "@id": "urn:door:entry",
        "@type": "top:Door",
        "geometry": {
          "host_wall_id": "urn:wall:south",
          "offset": 1800,
          "width": 1000,
          "hinge": "left",
          "swing": "in"
        }
      },
      {
        "op": "create",
        "@id": "urn:wall:south",
        "@type": "top:Wall",
        "geometry": {
          "start": [0, 0],
          "end": [10000, 0],
          "thickness": 200
        }
      }
    ]
  }
}
```

The local planner orders the host wall before the door and reports
`DEPENDENCY_ORDER_NORMALIZED` as `AUTO_FIXABLE`. CAD remains unchanged.

### Shared-room wall normalization

Room changes are expanded during local planning, not during CAD apply. The room
remains one non-plot semantic boundary while its physical boundaries join the
same wall-requirement set as explicit wall changes. Compatible centerlines are
direction-normalized on a 0.01 mm grid and merged before the execution plan is
created.

Two adjacent 4000 × 4000 mm clear rooms with 200 mm walls therefore preview two
room boundaries and seven physical walls. Their common partition has one stable
semantic ID, both room IDs in XData, and `top:boundedBy` relationships from both
rooms. A door may reference either legacy room-wall alias; the planner rewrites
it to the normalized shared host without another model call.

The compiler does not hide incompatible decisions. A coincident centerline with
different thickness, height, style, or representation produces
`WALL_SPEC_CONFLICT` / `NEEDS_LLM_DECISION` and no transaction is applied.

### 3. Stop for approval, then apply

```json
{
  "request": {
    "action": "apply",
    "task_id": "create-2bhk",
    "transaction_id": "...",
    "detail_level": "summary"
  }
}
```

Apply rechecks the drawing name and SHA-256 revision, executes the entire plan
inside one AutoCAD undo group, validates the new snapshot, and atomically writes
JSON-LD/Turtle sidecars. Reapplying the same ID is idempotent.

## Recommended modification workflow

First resolve only the kitchen neighborhood:

```json
{
  "request": {
    "action": "get_context",
    "task_id": "move-kitchen",
    "entity": "Kitchen",
    "budget": {
      "graph_depth": 1,
      "max_entities": 25,
      "max_neighbors": 12,
      "include_geometry": true,
      "include_metadata": true
    }
  }
}
```

The model decides the new approved geometry once. Submit all dependent updates
in one `modify` request, review the preview, and call `apply`. Do not use
`manage_files new` during this workflow.

## Compact responses and budgets

Response levels are `summary`, `normal`, `detailed`, and `debug`. `summary` is
the default. Geometry, full diffs, raw operations, and execution-step details
are omitted unless requested.

Context budgets support:

```text
max_entities   1..500
max_neighbors  1..100 per node
graph_depth    0..5
include_geometry
include_metadata
```

Truncated context returns a `result_id`. Use `get_result` with `offset` and
`max_entities` to page it without reconnecting to CAD or recomputing topology.

## Validation and failure policy

Problems are classified as:

- `AUTO_FIXABLE`: safely normalized locally, such as dependency order.
- `NEEDS_LLM_DECISION`: architectural conflict such as duplicate walls or
  overlapping proposed rooms, or incompatible shared-wall specifications.
- `FATAL`: invalid or conflicting semantic identity/geometry.

Errors include `code`, `stage`, `retryable`, `details`, `suggested_action`, and
an error fingerprint. Recognized transient AutoCAD busy/RPC errors reconnect
and retry once. Non-transient validation failures are never retried blindly.
After two identical failures, the same request is stopped.

## Cache and incremental behavior

The first version caches analysis by `(drawing name, SHA-256 revision)` and
reuses it across inspect, context, and preview calls. Any successful apply or
rollback invalidates prior revisions and caches the verified new revision.

Fine-grained graph mutation is deliberately deferred. Safe full snapshot and
analysis remain the fallback after CAD mutation because incorrect incremental
topology is worse than a slightly slower verified rebuild.

## Metrics

Reuse `task_id` across the workflow, then request:

```json
{"request": {"action": "metrics", "task_id": "move-kitchen"}}
```

Metrics include MCP calls, CAD operations, topology operations, retries,
failures, response bytes, execution time, entities inspected, and entities
modified. The server returns model token usage as unknown rather than inventing
it.
