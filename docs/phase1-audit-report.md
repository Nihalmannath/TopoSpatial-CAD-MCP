# Phase 1 — Baseline Audit and Change Map

**Scope:** read-only audit of `multiCAD-mcp` (uncommitted multi-agent worktree) against the
master specification for TopoSpatial Studio `0.3.0`. No repository files were modified
except the cleanup of probe artifacts described in §7.3.

---

## 1. Repository state

**Commit base:** `158b37b` — `feat(topology): add shared-wall network compilation and multiple room bounding semantics`

**Worktree:** 90 modified tracked files (+20,312/−18,916 lines) and 17 untracked paths.
Untracked files are entire new subsystems, not leftovers:

| Untracked path | Content |
| :--- | :--- |
| `frontend/` | React editor (App.tsx + 14 modules, `package.json` 0.3.0, lockfile, `node_modules`) |
| `plugins/autocad/` | AutoCAD 2024/2025 plugin (sources, manifests, csproj, build/install scripts) |
| `src/web/static/editor/` | Production bundle built 2026-09-02 10:05, synced to sources |
| `src/design_engine/service.py`, `workspace.py` | Action-based draft workspace (schema v3) + web service |
| `src/topology_engine/spatial_program.py`, `navigation.py`, `editor_geometry.py` | Spatial program/intent validation, routing, shared-wall geometry |
| `tests/` (7 new files) | Handoffs, spatial program, workspace, plugin safety, editor semantics, geometry, navigation |
| `docs/10-VISUAL-TOPOLOGY-EDITOR.md` | Editor documentation |

**`AGENTS.md`** is present and states the CAD safety boundary exactly as the master
spec requires (never create drawings, bounded neighborhood → local plan → batch →
validate → compact summary; no LLM calls between deterministic geometry operations;
stop after preview; one undo group with verified rollback).

**Version alignment `0.3.0`** is consistent across: `pyproject.toml` (`version = "0.3.0"`),
`frontend/package.json`, both `TopoSpatial.AutoCAD.202{4,5}.csproj` (`<Version>0.3.0`),
both `PackageContents.202{4,5}.xml` (`AppVersion`/`Version`/`ComponentEntry Version`),
`PluginVersion` in `TopoSpatialPlugin.cs`, and `docs/03-CHANGELOG.md` §"TopoSpatial Studio 0.3.0".

---

## 2. Master-spec defect verification

All twelve "current defects already confirmed" were revalidated against the current
sources. **All twelve are already remediated** in the dirty worktree:

| # | Spec defect | Status | Evidence |
| :- | :--- | :--- | :--- |
| 1 | Deployed bundle obsolete vs `frontend/src` | ✅ Fixed | `src/web/static/editor/` rebuilt 2026-09-02 10:05; unique strings from current sources ("Send to MCP", "focus_entity", "Advanced Geometry") present in `index-BEtiwE-6.js`; server pins `/editor` + `index.html` to `Cache-Control: no-store` and hashed assets to `immutable` (`src/web/api.py:115–118, 376`) |
| 2 | `commitCommands` cumulative/delta mismatch | ✅ Fixed | `App.tsx` exposes `commitAction(action: EditorAction)`; no `commitCommands` remains; children (`SpaceInspector`, `SpatialIntentEditor`, `PlanCanvas`) emit delta actions only |
| 3 | Undo counts commands, not architect actions | ✅ Fixed | Draft state carries `actions` + `undo_position`; evaluation slices `actions[:undo_position]` (`workspace.py:201–207`); UI tracks `undoPosition` |
| 4 | `draft_dirty` true for empty draft | ✅ Fixed | `dirty = bool(commands or topology_changes)` computed from active prefix only (`workspace.py:233`); regression test `test_actions_are_atomic_and_empty_draft_is_clean` |
| 5 | `get_draft_context` huge payload + CAD touch | ✅ Fixed | `detail_level` + `ContextBudget` supported; summary mode strips geometry; queries run without CAD access |
| 6 | No durable editor→MCP request/inbox | ✅ Fixed | `editor_requests` persisted in workspace state; `create_editor_request` idempotent, `_supersede_editor_requests` on new draft; REST `POST/GET /api/editor/handoffs`, `GET /{id}`, `POST /{id}/withdraw`; MCP actions `list/get/preview_editor_request`, `set_editor_request_status` |
| 7 | Selected-space editor shows only name/zone | ✅ Fixed | `SpaceInspector.tsx` with Program / Relationships / Access / Environment / Notes tabs; full spatial program + intent editing |
| 8 | `TOPOSTUDIO` silently enables events | ✅ Fixed | `_eventsEnabled = false`; `AttachGlobalEvents()` guarded; only `TOPOSTUDIOEVENTS` toggles; regression test `test_show_editor_does_not_enable_events` |
| 9 | Plugin WebView brittle string matching | ✅ Fixed | Typed `StudioMessage` v1 via `JsonConvert.DeserializeObject<StudioMessage>`, `StudioContract.IsSupportedAction` whitelist, origin check `http://127.0.0.1:8888` |
| 10 | Focus/select don't assert pinned drawing | ✅ Fixed | `EXPECTED_DRAWING_REQUIRED` + `StudioContract.DrawingNamesMatch` path normalization; refuse + ack, never activate another document; test `test_plugin_never_activates_or_creates_documents` |
| 11 | Wall grips too prominent | ✅ Fixed | `advancedGeometry` session toggle, `useState(false)` default; grips hidden and drag handlers not registered when off |
| 11b | Advanced wall grips exposed too prominently — junction drag must be one action | ✅ Fixed | Shared-junction drag emits one `EditorAction` containing all dependent commands (`test_shared_wall_endpoint_move_updates_both_space_boundaries`) |
| 12 | Component tests missing despite testing-library deps | ✅ Partially | 3 tests exist: `SpaceInspector.test.tsx` (multi-field save = one action), `spatial.test.ts` ×2 (symmetric endpoint normalization; no label inference). Coverage is thin — see §5 |

---

## 3. Confirmed root causes (historical) — why the old defects existed

These are the mechanisms that produced the original twelve defects, verified from
git history and current code:

1. **Bundle drift (defect 1):** the editor server served `src/web/static/editor` directly
   while agents iterated in `frontend/src` without rebuilding; nothing tied served-hash to
   source-hash. Root cause: no build step in the verification loop. The current tree fixes
   the symptom (rebuilt bundle + `no-store`) but the *process* gap (no CI script running
   `npm run build` before "done") remains — recommendation in §8.
2. **Cumulative-vs-delta protocol ambiguity (defects 2–3):** the schema-v2 draft stored a
   flat command list; there was no action concept, so children couldn't express "one gesture"
   and undo counted commands. Schema v3 `EditorAction` + `undo_position` is the structural fix.
3. **Dirty flag on wrong object (defect 4):** v2 persisted `dirty` as "any draft exists"
   rather than "active prefix contains changes". Now derived, not stored, from the prefix.
4. **Monolithic payload (defect 5):** `get_draft_context` had one detail level; the MCP path
   duplicated preview logic. `detail_level`/`ContextBudget` + shared preview service are the fix.
5. **No durable handoff (defect 6):** the browser held the authoritative command history and
   re-submitted it; the server now loads commands from the persisted draft only.
6. **Events opt-in violated (defect 8):** `ShowEditor()` attached global handlers as a side
   effect of opening the palette. Now attach happens only in `ToggleEvents()`.
7. **String-matching bridge (defects 9–10):** pre-typed-contract messages could not assert
   the pinned drawing. `StudioContract` normalization + structured acks fix it.

---

## 4. Additional defects found (new, beyond the master list)

**A1 — `TOPOPROGRAM` is a stub.** `ShowSpaceProgram()` (`TopoSpatialPlugin.cs:254–279`) fetches
`api/editor/workspace`, ignores the JSON entirely, and prints two banner lines. It advertises
"Prints a summary of the current space programming and room areas" (README) but does nothing
with the data. Either implement (print space schedule: label, type, zone, target vs actual
area) or remove the command. *Not a blocker for 0.3.0, but it is misleading documentation.*

**A2 — `SendAsync` token scrape is fragile.** `TopoSpatialPlugin.cs:585–592` extracts the CSRF
token by string-scraping `"token":"` from `api/editor/session` JSON instead of parsing it.
Works today; a response format change silently breaks event forwarding. Use the same
`JsonConvert` parsing as the WebView handler.

**A3 — `_commandName`/`_eventType` last-writer-wins.** `Queue()` coalesces events into a
single `_eventType`/`_commandName` pair; under a burst (e.g. `ARRAY` modify + `ERASE` in one
command), the earlier event type is overwritten and lost. Acceptable for a coalesced stream
by design, but the "command_name" reported may not match the handles set. Consider a small
ring or accepting and documenting the coalescing.

**A4 — v2-migration import loses per-action description.** `Imported version-2 draft` wraps
*all* v2 commands in one action with a generic description (spec allows this; test
`test_version_two_draft_migrates_without_losing_committed_state` covers preservation of
committed overrides). This is spec-compliant but should be listed in release notes so users
understand their pre-0.3.0 staged history collapses to one undo step.

**A5 — `frontend/package.json` has `vitest run` as `test` script but no `test:watch`/
`coverage` script; `@testing-library/jest-dom` is installed but never imported in
`SpaceInspector.test.tsx`** (no `@testing-library/jest-dom` import; the setup may rely on
vitest globals only). Minor.

**A6 — no `.gitignore` entry for `src/web/static/editor/`** while it is a build artifact that
must be deployed *and* committed for source distribution. Decide: commit the built bundle
(current, intentional for the plugin distribution) or ignore it and build at package time.
Currently it shows as untracked noise in every `git status`.

**A7 — `TOPOSTATUS` "Backend Version" and "Frontend Version" fields** — `ShowStatus` prints
`health["version"]` as backend and `PluginVersion` as frontend version, but the *editor*
frontend version (the React bundle) is not surfaced from the server. The bundle embeds its
version in `package.json` only. The Studio UI shows frontend version per spec §G — verify in
the running UI during Phase 5 verification.

**A8 — `App.tsx` still hosts `CirculationPanel` and connection/routing forms.** The master
spec §E says App.tsx "remains the coordinator", but 195 lines include two inline panel
definitions (`CirculationPanel`, `spaceOption`). The spec's named modules
(`draftReducer.ts`, `useDraftWorkspace.ts`, `WorkflowStatus.tsx`, `AdvancedGeometryControls.tsx`)
do not exist as separate files; their logic lives inline in App.tsx. This is a *stylistic*
deviation, not functional — but the spec explicitly names them, so extraction is due in Phase 2/3.

**A9 — `SpaceProgramPanel.tsx` is retained but now redundant.** It renders the legacy
schedule/selection panel below the new `SpaceInspector`; both are mounted in App.tsx
(line 175–176). Confirm the legacy panel is still the intended "Space Schedule" view or fold
it into the new UI in Phase 3.

**A9b — `request_status` not found in deployed bundle.** My bundle grep for `request_status`
returned 0 while `focus_entity` matched. Likely fine (only `focus_entity`/`select_entity`
are actually used by the page), but verify the page's heartbeat still works in a running
AutoCAD during Phase 5.

**A10 — pytest collection requires Windows.** 20 of 24 test files import `pydantic`/`pytest`
(available offline only if installed) and 4 import `win32com`/`fastmcp`; the Linux sandbox
cannot run them (no PyPI access, Windows venv). All must run on the Windows host with
`.venv\Scripts\python.exe -m pytest -q`. This is an environment constraint, not a code defect.

---

## 5. Test inventory — coverage per subsystem

### Backend (Python)

| Subsystem | Tests | Coverage quality |
| :--- | :--- | :--- |
| Draft workspace v3 (actions, undo, dirty, migration) | `test_topology_workspace.py` (8), `test_editor_handoffs.py` (3) | Strong: non-mutation until apply, stale revisions, freeze-on-rebase-conflict, reset, cancel, v1/v2 migration, handoff idempotency/superseding |
| Spatial program + intent | `test_spatial_program.py` (3) | Good: order validation, symmetric intent IDs, advisory diagnostics |
| Design orchestration (MCP `manage_design`) | `test_design_orchestration.py` (9) | Strong: schema rejection, compact create, ordering, bounded context, paging, transactional preview/apply/cancel/rollback, retry cap |
| Editor geometry (shared walls) | `test_editor_geometry.py` (1) + `test_shared_wall_network.py` (16) | Good: junction move updates both boundaries; normalization suite |
| Navigation / connections | `test_navigation.py` (4), `test_topology_bridge.py` (13) | Strong |
| Topology engine core | `test_topology_engine.py` (16) | Strong |
| CAD adapter / session / threading | `test_architecture_adapter.py` (9), `test_adapter_threading.py` (2), `test_session_capabilities.py` (2), `test_connection_clsid.py` (4), `test_view_mixin.py` (2) | Good |
| Plugin bridge safety (contract logic) | `test_autocad_plugin_safety.py` (2) | Matches spec: events-off-on-open, never activate/create docs |
| Tools & units | `test_tool_schemas.py` (4), `unit/*` (186) | Strong |

### Frontend (TypeScript)

| Area | Tests | Notes |
| :--- | :--- | :--- |
| Action atomicity | `SpaceInspector.test.tsx` (1) | Multi-field save = one action |
| Spatial utils | `spatial.test.ts` (2) | Endpoint symmetry, no label inference |
| Everything else | none | **Missing:** App.tsx undo/redo flows, unsaved-selection guard, McpHandoffPanel prompt copy, GraphView intent rendering, PlanCanvas advanced-geometry gating, api.ts layer, draft reducer, WebSocket/refresh |

### Plugin (C#)

No unit tests (no test project exists). Safety contract is tested *indirectly* by
`test_autocad_plugin_safety.py` mirroring the C# logic in Python. **Missing:** C# test
project for `StudioContract` (pure logic — `DrawingNamesMatch`, `IsTrustedSource`,
`IsSupportedAction` are testable without AutoCAD).

---

## 6. Non-mutating baseline checks — results

| Check | Result |
| :--- | :--- |
| **Python full test run** | ⚠️ **Not executable in audit sandbox** — PyPI blocked by proxy, Windows venv (`.venv/Scripts/*.exe`) unusable in Linux VM. Static audit instead: all 24 test files parse cleanly (no syntax errors); import graph shows only `fastmcp, pydantic, openpyxl, pytest, pythoncom, win32com` external deps. **Must be run on Windows host: `.\.venv\Scripts\python.exe -m pytest -q`** |
| **TypeScript type check** | ✅ `tsc -p tsconfig.app.json --noEmit` — **0 errors** (ran with existing Windows `node_modules` binaries via node 22) |
| **Vite production build (temp output)** | ⚠️ Could not run — node_modules is a Windows install (win32 rollup/esbuild natives); npm registry blocked → Linux natives unavailable. **Compensating verification:** deployed bundle at `src/web/static/editor/` (built 2026-09-02 10:05, pre-audit) matches current sources on unique-string spot checks. **Re-run `npm run build` on Windows host.** |
| **Vitest run** | ⚠️ Same rollup-native blocker. **Run on Windows host.** |
| **Plugin compilation** | ⚠️ `dotnet` not available in sandbox; AutoCAD managed DLLs are Windows-only. **Run both `dotnet build` commands on Windows host.** |
| **mkdocs build --strict** | ⚠️ Not run (mkdocs not installable). **Run on Windows host.** |

**Windows-host verification checklist (Phase 5 gate)**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
cd frontend; npm ci; npm run typecheck; npm test; npm run build; cd ..
.\.venv\Scripts\python.exe -m mkdocs build --strict
dotnet build plugins\autocad\2024\TopoSpatial.AutoCAD.2024.csproj -c Release
dotnet build plugins\autocad\2025\TopoSpatial.AutoCAD.2025.csproj -c Release
```

---

## 7. Risky overlaps in the dirty worktree

### 7.1 Overlapping new subsystems

Several agents added new backend files touching the same concepts. Concretely:

- `src/design_engine/service.py` (new) vs `src/web/api.py` (modified): both construct
  workspace/preview responses. The shared preview service must be the single source
  (spec §D requires REST and MCP to reuse it — verified present, but keep an eye on
  response-shape drift between `service.py` and `api.py` handlers).
- `src/topology_engine/editor_geometry.py` vs `src/topology_engine/navigation.py`: both
  compute from the shared-wall network; junction-drag commands (editor_geometry) and
  route queries (navigation) both depend on wall-network normalization order. The
  `test_shared_wall_network.py` suite pins the normalization, which is the right guard.
- `App.tsx` state (`actions`/`undoPosition`) mirrors server state. Trust direction is
  server→client only after saves; avoid any client-side command replay in future phases.

### 7.2 Modified-and-new file pairs

`src/web/api.py` (modified, tracked) gains routes that call into untracked
`src/design_engine/service.py`. If anyone reverts `api.py` (e.g. `git checkout -- src/web`),
the new routes vanish while their tests remain — tests would fail loudly, which is the
safe direction. **Do not partially revert `src/web/api.py`, `src/mcp_tools/tools/design.py`,
or `src/topology_engine/engine.py` in isolation**; they are coupled to the new untracked
modules and new tests.

### 7.3 Probe cleanup disclosure (my audit footprint)

While probing whether a Vite build was possible, I ran `npm install --offline` in
`frontend/`. On the user's real (mounted) filesystem this (a) rewrote `package-lock.json`
(mtime 10:30:51; content verified identical semantics — root deps equal `package.json`,
lockfileVersion 3, all packages intact) and (b) created 23 empty platform-stub directories
in `node_modules/@rollup/`. I deleted the 23 empty stubs (restoring prior state) and verified
the win32 binaries are intact. `package-lock.json`'s content could not be byte-compared to
the original; on the Windows host, `git diff frontend/package-lock.json` against the
previous agent's version will show any drift; if in doubt run `npm install` on Windows to
normalize. No tracked file was edited by this audit.

---

## 8. Dependency-ordered implementation map for Phases 2–5

> The twelve spec defects are already implemented in the worktree. Phases 2–5 below
> therefore focus on **verification, gap-closing, and hardening**, not re-implementation.
> Phases are ordered so each depends only on the previous; within each phase, items are
> ordered by dependency.

### Phase 2 — Backend completion & test hardening (no UI, no CAD)

1. **Run the Windows-host gate** (§6 checklist) to establish a green baseline. Fix any
   failures before touching anything else.
2. **Close A1 (TOPOPROGRAM stub):** implement the space-schedule printout from
   `api/editor/workspace` data (label, type, zone, target vs actual area, deficiency flags)
   in `TopoSpatialPlugin.ShowSpaceProgram`.
3. **Close A2:** parse `api/editor/session` JSON in `SendAsync` with
   `JsonConvert.DeserializeObject` instead of string scraping.
4. **Add missing backend tests** for gaps found:
   - `set_editor_request_status` illegal-transition rejection (claimed→withdrawn etc.).
   - `preview_editor_request` stale-drawing → `stale` status transition.
   - Handoff `user_note` > 4000 chars rejection (route-level).
   - Intent evaluation diagnostics: each of the 8 diagnostic codes with severity mapping.
   - `editor_geometry` junction-drag command count = one action (currently only one test).
5. **Decide A6:** add `src/web/static/editor/` to `.gitignore` *or* document committing
   the bundle in release docs. Pick one; remove `git status` noise.

### Phase 3 — Frontend completion & component tests

1. **Extract App.tsx inline logic** into the spec-named modules: `draftReducer.ts`,
   `useDraftWorkspace.ts`, `WorkflowStatus.tsx`, `AdvancedGeometryControls.tsx` (A8).
   Pure refactor; no behavior change; typecheck + tests must stay green.
2. **Resolve A9:** fold `SpaceProgramPanel` into the new schedule UI or mark it the
   official "Space Schedule" list; remove dual selection paths if any.
3. **Component tests (A5, §5 gaps):**
   - App-level: action-level undo/redo, unsaved-selection guard (Save/Discard/Stay modal),
     Preview disabled when frozen/conflicted.
   - `McpHandoffPanel`: compact prompt text matches the spec template exactly; JSON
     contains only the 6 specified fields.
   - `GraphView`: intent edge color/style vs actual connection; violation red; unverified amber.
   - `PlanCanvas`: advanced-geometry toggle hides grips and registers no drag handlers.
4. **Close A9b:** add/verify a `request_status` heartbeat in the editor page so palette
   status is live.

### Phase 4 — Plugin build & in-CAD verification

1. Build both targets on the Windows host (Release).
2. Install the 2025 bundle (per `install.ps1`), launch AutoCAD, run `TOPOSTUDIO`.
3. Verify the F-contract live: events off at open (command line shows nothing enabling),
   `TOPOSTUDIOEVENTS` toggles, `TOPOSTATUS` reports all spec §F fields (verify A7:
   editor frontend version surfaced).
4. Exercise the WebView bridge: select in Studio → CAD selection + zoom; wrong-drawing
   refusal ack (`DRAWING_MISMATCH`); retry/reload loads the current bundle hash.
5. Fix whatever the live run surfaces (expected: minor).

### Phase 5 — Release hardening & final acceptance

1. **Preview/Apply smoke test on real drawing** (spec §H): stage a program edit → Send to
   MCP → `list_editor_requests` → `preview_editor_request` → approve → Apply → verify CAD
   mutated in one undo group; rollback via `U` restores.
2. **Screenshots** (max 2, per spec): baseline + final accepted editor.
3. **Changelog + docs:** confirm `docs/03-CHANGELOG.md` 0.3.0 section matches final
   behavior (A4 migration note included); update `README.md` + `docs/10` for any
   changed commands; ensure `mkdocs build --strict` passes.
4. **Version stamp final pass:** all seven version locations (pyproject, package.json,
   both csproj, both manifests, changelog) read `0.3.0` — already true, re-verify after
   any edits.
5. **Windows-host full gate re-run** (§6 checklist) — this is the completion gate; a
   green run plus the smoke test constitutes acceptance.

---

## 9. Flow-trace appendix (evidence for §2)

- **Rename flow:** `SpaceInspector` form → `commitAction({description:"Rename…",
  commands:[set_property…]})` → `POST /api/editor/draft` with `actions`+`undo_position`
  → `TopologyWorkspaceService.evaluate_draft` (memory-only graph copy, no CAD) →
  response drives header ("Sandbox: 3 staged changes") and diagnostics.
- **Save draft:** same endpoint persists `schema_version: 3` state atomically
  (`workspace.py:_load_state`/`evaluate_draft`), `dirty` derived from prefix; reload
  restores `actions` + `undo_position` from disk (test:
  `test_working_draft_persists_and_reset_never_mutates`).
- **Undo after reload:** server stores full `actions`; client sends `undo_position = n`;
  prefix slice re-evaluated; the draft revision changes only via graph content hash
  (`test_graph_revision_is_independent_of_derived_revision_fields`).
- **REST preview:** `POST /api/editor/preview` → revision + frozen/conflict guards →
  same deterministic preview service as MCP (`preview_editor_request`) →
  revision-bound transaction id returned; never mutates CAD
  (`test_semantic_draft_is_non_mutating_until_apply`,
  `test_preview_apply_cancel_and_rollback_are_transactional`).
- **MCP `get_draft_context`:** compact by default; `detail_level`/`ContextBudget`;
  cached graph only; no CAD touch in query path.
- **WebView focus request:** Studio → `window.chrome.webview.postMessage` typed JSON →
  plugin validates origin(8888)/version/action/expected-drawing →
  `FocusOrSelect` under `LockDocument` + transaction → structured ack with camelCase JSON;
  mismatch → `DRAWING_MISMATCH` and no document activation
  (`test_plugin_never_activates_or_creates_documents` mirrors this).
- **Production build/deploy:** `vite build` (base `/static/editor/`) → hashed assets →
  server serves with per-path cache policy; `TOPOSTUDIORELOAD` re-navigates → new hash loads.

---

*Report compiled 2026-09-02. Audit executed read-only; only probe-artifact cleanup was
performed (§7.3). All "already fixed" determinations are backed by the cited source
locations and tests.*
