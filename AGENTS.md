# TopoSpatial-CAD-MCP Agent Rules

These rules apply to coding agents and CAD-controlling agents working in this
repository.

## Modification workflow

For modification requests, **never create a new drawing unless the user
explicitly asks for a new drawing**. Modify the current drawing in place through
preview/apply transactions.

Before invoking low-level drawing tools:

1. Identify the affected semantic entities.
2. Obtain only their bounded topology neighborhood.
3. Create one local execution plan.
4. Batch dependent CAD mutations.
5. Validate locally.
6. Return a compact summary.

Do not call the LLM between deterministic geometry operations. Do not rerun full
topology analysis when the current drawing revision is already cached.

Capture or send images only at meaningful visual checkpoints, preferably once
after preview and once after the final modification.

Stop after preview whenever user approval is required. Apply only the approved,
unexpired transaction and recheck the drawing revision before mutation.

## CAD safety boundary

- Keep AutoCAD COM objects inside the adapter and on their owning thread.
- Pass plain serializable geometry to topology and analysis services.
- Keep existing low-level tools available for debugging and unsupported cases.
- Never leave partial CAD changes after a failed batch; use one undo group and
  verify rollback.
- Do not infer architectural intent that the user or controlling model has not
  approved.
