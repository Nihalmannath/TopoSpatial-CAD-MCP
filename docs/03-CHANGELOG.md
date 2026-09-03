# Changelog

## [3.0.0] - 2026-09-03

### Added

#### AutoCAD Plugin (.NET & WebView2)
- Native AutoCAD and AutoCAD Architecture plugin packages in `plugins/autocad`:
  - **2024 Release**: Compiled against .NET Framework 4.8.
  - **2025 Release**: Compiled against .NET 8 (Windows Desktop).
- `TOPOSTUDIO` dockable palette hosting the interactive visual topology studio via Microsoft WebView2.
- `TOPOSTUDIOEVENTS` non-invasive document and entity change tracker that coalesces modifications on CAD idle events without opening competing transactions.
- Typed message protocol ensuring secure, origin-validated bidirectional communication between the web studio and the CAD document.
- Single-threaded apartment (STA) COM worker (`com_worker.py`) with fast-failing circuit breaker protecting CAD from RPC contention or modal lockups.

#### Visual Topology Studio & Spatial Graph
- Full-featured React 18 + TypeScript + Vite topology editor bundled directly into the MCP web server at `/editor`.
- Dual synchronized viewport: full-bleed vector CAD underlay canvas paired with a Cytoscape.js circulation and topological graph.
- Spatial intent authoring (`top:SpatialIntent`) supporting programmatic architectural constraints (direct access, adjacency, isolation) without premature CAD linework.
- Real-time circulation graph routing (`/api/editor/route`) with door/opening clearance verification and accessibility checks.
- Architectural space scheduling, program inspection, and zone classification (living, sleeping, service, circulation, office, outdoor).
- WebSocket event broker (`/api/editor/events`) streaming live model and CAD updates to connected visual clients.
- Process-local session token gating and single-dashboard port ownership protocol.

#### High-Level Design Orchestration & Planning
- `manage_design` high-level lifecycle with local execution planning: `create`, `modify`, `validate`, `preview`, `apply`, `cancel`, `rollback`, `inspect`, and `metrics`.
- Deterministic shared-wall compiler resolving multi-room partition boundaries into single physical walls with multiple `cad:boundingRooms`.
- Host-before-opening local dependency graph enforcing wall creation prior to doors or windows.
- Revision-keyed topology cache and bounded semantic-neighborhood walker.
- Support for arbitrary 2D polygon boundaries for rooms and spaces (`top:Space`, `top:Room`).
- In-place transactional preview/apply with automated single-undo-group rollback on failure.


- Typed `manage_design` high-level lifecycle with `inspect`, `get_context`,
  `get_result`, `create`, `modify`, `validate`, `preview`, `apply`, `cancel`,
  `rollback`, and `metrics` actions.
- Local `ExecutionPlan` generation, host-before-opening dependency ordering,
  affected semantic ID reporting, and one preview transaction for a complete
  approved architectural batch.
- Revision-keyed topology analysis cache and a bounded semantic-neighborhood
  walker with process-local pagination for truncated context.
- `summary`, `normal`, `detailed`, and `debug` response levels; summary is the
  compact default.
- Local problem classes (`AUTO_FIXABLE`, `NEEDS_LLM_DECISION`, `FATAL`) with
  duplicate-wall, overlapping-room, and semantic-ID validation.
- Structured failure responses with error codes, stages, retryability,
  suggested actions, and stable fingerprints.
- One-attempt retry/reconnect policy for recognized transient AutoCAD busy/RPC
  failures and repeated-failure stopping after two identical attempts.
- Per-task proxy metrics for MCP calls, CAD/topology operations, retries,
  failures, response bytes, execution time, and inspected/modified entities.
- Repository-level `AGENTS.md` enforcing in-place modification, bounded context,
  local planning/validation, meaningful image checkpoints, and approval before
  apply.
- Design orchestration and efficiency benchmark documentation.
- Optional `topology` dependency group pinned to TopologicPy 0.9.65 and
  topologic-core 8.0.4.
- `manage_topology` for explicit 2D room, wall, door, and window semantics;
  relationship queries; preview/apply transactions; and JSON-LD/Turtle sidecars.
- `TOPOSPATIAL_TOPOLOGY` XData schema and non-plot `AI-ROOMS` boundaries.
- Runtime AutoCAD Architecture capability and style discovery.
- Native `AecDbWall`, `AecDbDoor`, and `AecDbWindow` creation with native
  opening-to-wall anchors and portable standard-entity fallback.
- `manage_session` `capabilities` action for product, AEC API, and style details.
- `auto`, `native_aec`, and `standard` representation policies, resolved and
  frozen during preview.
- Thread-neutral CAD snapshots and a dedicated topology analysis worker.
- Tests for native capability discovery, JSON-LD relationships, candidate
  suppression, deferred refresh, stale previews, rollback, and ACA window
  discovery.
- A complete native ACA/topology workflow guide with a tested 5000 × 4000 mm
  clear-room example.
- Confirmed `manage_files` `delete` action for exact closed `.dwg`/`.dxf`
  outputs. It optionally includes matching topology sidecars and uses the
  Windows Recycle Bin.
- Deterministic shared-wall compiler that expands room boundaries during
  planning, merges compatible implicit/explicit requirements, assigns stable
  geometry-derived wall IDs, and rewrites legacy opening hosts.
- XData schema version 2 multi-room wall ownership plus `top:boundedBy` and
  `top:bounds` JSON-LD/Turtle relationships.
- Shared-wall regression coverage for adjacent rooms, reversed/noisy geometry,
  specification conflicts, explicit/implicit unification, opening hosts,
  representation selection, apply counts, stable IDs, and re-analysis.

### Changed

- `manage_session` now advertises a typed discriminated native object/array
  schema while retaining JSON-encoded strings for backward compatibility.
- Drawing, entity, layer, block, and file batch tools now advertise native MCP
  object/array inputs as well as shorthand strings.
- Boolean autocorrection preserves invalid structured values so `{}` cannot be
  silently converted to `false`; typed validation now reports the mismatch.
- Topology legacy payload validation is action-specific and errors are
  machine-actionable. `manage_topology` remains available for compatibility;
  agents should prefer `manage_design` for architectural tasks.
- Preview transactions now support cancellation and record execution metadata;
  applied transactions can be rolled back only while the CAD revision proves
  they are still the latest safe change.
- Adapter registry and active adapter context are thread-local so COM objects do
  not cross MCP, dashboard, and topology worker threads.
- Session status reconnects on the calling worker and no longer reports a valid
  base CAD connection as disconnected when optional capability probing fails.
- Topology apply defers refresh until post-validation, then atomically updates
  sidecars.
- Completed transaction reapply is explicitly idempotent.
- Managed clear-room boundaries suppress duplicate wall-envelope candidates.
- Room apply now creates only the semantic boundary; every physical wall is an
  explicit previewed operation from the normalized wall network.
- Opening membership uses `top:isPartOf`; `cad:hostWall` remains a scalar planner
  identifier.
- Shared walls persist multiple bounding rooms while single-room walls retain
  legacy `parent_id` compatibility.
- CAD screenshot capture now uses the live COM HWND, supports ACA's MFC window
  class, renders obscured windows with `PrintWindow`, and handles high-DPI bounds.
- **Table Entity Support**: Added native table creation through `draw_entities`
  (`table`, shorthand alias `tab`).
- **Arbitrary output paths**: Added the opt-in `output.allow_arbitrary_paths`
  setting; safe configured output paths remain the default.
- **Selection mapping for tables**: Registered `table` as `AcDbTable`.
- `manage_files save` now reports the actual `drawings` subdirectory path used
  by the adapter.

### Fixed

- MCP clients receiving `expected string, received array` for native batch and
  session operation arrays.
- Agents repeatedly rebuilding the unchanged topology graph between inspect,
  context, and preview calls.
- Opening-before-host ordering in otherwise valid multi-object design batches.
- Duplicate physical and semantic walls created independently by adjacent room
  operations.
- Explicit walls and implicit room walls bypassing one another during duplicate
  validation.
- Post-apply JSON-LD serialization failure caused by assigning both a scalar and
  relation list to `cad:hostWall`.
- Rollback returning before AutoCAD completed its asynchronous undo command.
- False room candidates caused by native wall centerline envelopes around an
  explicitly managed clear-interior room.
- AutoCAD Architecture screenshots capturing another foreground application or
  only the upper-left quadrant at 200% display scaling.
- Agents being unable to remove MCP-created drawings because `manage_files`
  exposed no deletion workflow.
- Type-checking warning in `DrawMLeaderRequest` where `text_height` could resolve
  to `Any | None` instead of `float`.

### Verified

- Live AutoCAD Architecture smoke test: capability discovery → analyze → preview
  → native apply → idempotent reapply → query → export → save.
- Example output: 4 `AecDbWall`, 1 `AecDbDoor`, 2 `AecDbWindow`, one semantic
  room boundary, 8 XData-tagged objects, 8 graph nodes, 14 relations, and zero
  duplicate candidates.
- Full suite: **286 tests passed** on 2026-09-01.
- Schema, cache, context pagination, compact response, retry, repeated-failure,
  cancellation, rollback, and 3-call create/room/opening workflow regressions.

---

## [0.2.0] - 2026-03-14

### Security (CRITICAL)

- **Path Traversal Prevention**: Added `_validate_export_path()` to prevent directory traversal attacks in file export operations.
- **Command Injection Mitigation**: Added `_sanitize_command_input()` to sanitize CAD command inputs, preventing malicious command injection.
- **Thread-Safe Singletons**: Implemented double-checked locking pattern in `AdapterRegistry` and `ConfigManager` for thread-safe operation.
- **COM Initialization Safety**: Improved error handling in `connection_mixin.py` for COM initialization across threads.

### Added

- **Block attribute management**: `get_attrs` and `set_attrs` actions in `manage_blocks` — read and write attribute tag values on block references.
- **Modern packaging**: `pyproject.toml` with full project metadata, dev/docs dependency groups, `[tool.ruff]`, `[tool.mypy]`, `[tool.interrogate]`, and `[tool.pytest]` configuration.
- **MkDocs documentation site**: Material theme with auto-generated API reference via mkdocstrings.

### Changed

- **Unified tool architecture**: 55 specific CAD commands replaced by 7 unified dispatch tools using compact shorthand format (~85% token reduction).
  - `manage_session` (11 actions), `draw_entities` (10 types), `manage_blocks` (6 actions), `manage_layers` (9 actions), `manage_files` (5 actions), `manage_entities` (10 actions), `export_data` (4 combinations).
- **Auto-named exports**: Excel export defaults to `[drawing_name]_data.xlsx` instead of `drawing_data.xlsx`.
- **Excel improvements**: autofilter enabled on all sheets (Entities, Layers, Blocks); `limit=0` ensures full export.
- **Dashboard refactor**: removed background refresher thread; export and refresh run directly on MCP thread; centralized configuration in `config.json` (port 8888).
- **Test suite**: expanded from 62 to 171 tests.

### Performance

- **O(n*m) → O(1) Optimization**: Optimized entity lookup in `set_entities_color_bylayer()` using `HandleToObject()` API.
  - Replaced inefficient nested loop iteration with direct handle-to-object lookups.
  - Expected 60%+ improvement on drawings with 10,000+ entities.

### Bug Fixes

- **Missing Return Statement**: Fixed `_paste()` function missing return value in `entities.py`.
- **Hardcoded Version**: Updated `web/api.py` to import version from `__version__.py` instead of hardcoding.
- **JSON Error Handling**: Added JSON error handling in `_set_color_bylayer()` with proper error messages.
- **Coordinate Validation**: Improved coordinate parsing with better error messages in paste operations.

---

## [0.1.3] - 2026-02-12

### Changed - Mixin Architecture Refactor

Major refactoring of the adapter layer for better maintainability.

#### Architecture

- **Mixin-based adapter**: `autocad_adapter.py` reduced from 3,198 to 99 lines.
- **11 specialized mixins**: Each mixin handles a specific responsibility (Utility, Connection, Drawing, Layer, File, View, Selection, Entity, Manipulation, Block, Export).
- **AdapterRegistry**: Encapsulated global state in singleton class.
- **Removed NLP**: Natural language processor removed (use direct tool calls).

#### Bug Fixes

- Fixed `@staticmethod` error in `validate_lineweight`.

#### Improvements

- **Refactored `DrawingMixin`**: Reduces boilerplate code in drawing methods using `_finalize_entity` helper.
- **Simplified `draw_mleader`**: Extracted complex fallback logic to improve readability.
- **Documentation**: Simplified `README.md` and updated documentation structure.

---

## [0.1.2] - 2025-12-09

### Added

- **Block creation**: `create_block` tool (from handles or selection).
- Core methods: `create_block_from_entities()`, `create_block_from_selection()`.
- 7 new tests (42 total).

### Changed

- Direct instantiation: `AutoCADAdapter(cad_type)` replaces factory.
- Context managers: `com_session()`, `SelectionSetManager`.
- Performance: `PickfirstSelectionSet` for fast entity access.

---

## [0.1.1] - 2025-11-22

### Added - Batch Operations

**13 batch operation tools** (legacy tools replaced by current unified architecture in 0.2.0):
- Drawing: `draw_lines`, `draw_circles`, `draw_arcs`, `draw_rectangles`, `draw_polylines`, `draw_texts`, `add_dimensions`.
- Layers: `rename_layers`, `delete_layers`, `turn_layers_on`, `turn_layers_off`.
- Entities: `change_entities_colors`, `change_entities_layers`.

---

## [0.1.0] - 2025-11-12

### Initial Release

- **Multi-CAD support**: AutoCAD, ZWCAD, GstarCAD, BricsCAD.
- **FastMCP 2.0** server with MCP tools.
- **Universal adapter** via COM API.
- **Excel export** with locale support.
- **Type safety**: 100% type hints.
- **Testing**: Comprehensive test suite.
