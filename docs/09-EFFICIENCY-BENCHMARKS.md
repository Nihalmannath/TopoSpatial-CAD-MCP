# 09 - Round-Trip and Response Benchmarks

## Measurement rules

These benchmarks count user-level MCP calls and local operations. They do not
claim model token usage because the MCP server cannot observe it. The legacy
comparison uses the existing public tool boundaries and includes a final
verification when the legacy apply result did not already provide the needed
compact context.

Two legacy behaviors matter:

- **Efficient legacy batch:** an expert manually batches `manage_topology` and
  `draw_entities` calls.
- **Micro-call agent behavior:** an agent repeatedly calls one low-level action
  per wall/opening/entity. This is the reported failure mode and can exceed 30
  calls for a 2BHK, but it is not used as the only baseline.

## Protocol comparison

| Workflow | Efficient legacy sequence | New sequence | Major calls before → after |
|---|---|---|---:|
| A. Inspect an existing plan | topology analyze, optional query | design inspect | 1–2 → 1 |
| B. Create semantic 2BHK shell/openings | analyze, topology preview, apply, final analyze | design inspect, create preview, apply | 4 → 3 |
| B2. Add furniture/dimensions/labels | previous sequence + one optimized `draw_entities` batch | previous sequence + one `draw_entities` batch | 5 → 4 |
| C. Move/modify one room | full query/analyze, preview, apply, final verification | bounded context, modify preview, apply | 4 → 3 |
| D. Modify a hosted door/window | full query/analyze, preview, apply, final verification | bounded context, modify preview, apply | 4 → 3 |

The automated integration tests enforce three major calls for semantic create,
room modification, and hosted-opening modification. Inspect requires one call.
The complete workflow therefore remains within the requested 3–6-call budget
without hiding validation or approval.

Wall counts are also explicit now. The adjacent-two-room regression previews
two semantic room boundaries and seven physical wall operations; apply receives
the same seven operations. Previously, preview showed two room operations while
the CAD bridge silently manufactured eight walls during apply.

## Local topology work

With the old tool, analyze and preview each rebuild topology. With
`manage_design`, inspect/context populates the revision cache and preview reuses
it. Apply still performs a full verified post-mutation analysis by design.

```text
Old create lifecycle: analyze + preview analysis + apply verification = 3 builds
New create lifecycle: inspect analysis + preview cache hit + apply verification = 2 builds
```

## Response-size fixture

The deterministic one-wall preview fixture produces:

| Detail mode | Serialized response |
|---|---:|
| `summary` | 458 bytes |
| `debug` | 1,772 bytes |

This is a test-fixture measurement, not a promise for every drawing. The result
shows why summary mode is the default: raw operations and full execution-plan
steps remain available without being sent on every call.

## Correctness guardrails

The benchmark does not remove:

- revision checks,
- preview approval,
- native ACA style/host validation,
- one undo-group apply,
- post-apply topology verification,
- rollback on failure,
- atomic sidecars, or
- agent access to detailed/debug output when needed.
