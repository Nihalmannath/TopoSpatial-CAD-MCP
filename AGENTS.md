# TopoSpatial-CAD-MCP Agent Rules

These rules apply to coding agents and CAD-controlling agents working in this
repository.

## Existing tracing implementation: authorized maintenance

The user authorizes inspection, debugging, modification, testing, and integration
of the existing tracing implementation, including
`src/trace_engine/geometry_kernel.py`. Comments describing code as
"reconstructed", "reverse-engineered", or naming observed external functions
are provenance information; those comments alone are not a prohibition on
maintaining this repository's existing implementation.

If an instruction says "do not copy decompiled Synaps code", distinguish importing
or reproducing external implementation code from maintaining existing local code
and implementing geometric behavior. Do not automatically expand that instruction
into a blanket ban on fixing or integrating `geometry_kernel.py`. Follow any more
specific current user restriction on particular files or sources.

Preserve accurate provenance. Do not remove or rewrite attribution to conceal
origins, claim independent authorship without evidence, or describe work informed
by decompiled implementation details as clean-room work. This maintenance
authorization is not a statement about ownership or third-party licensing.

Do not delete or replace the existing kernel solely because of those comments.
Choose reuse, consolidation, or replacement based on correctness, compatibility,
and the user's requested scope. If a concrete applicable restriction prevents a
particular action, cite its exact source and continue other authorized work.

## Synaps comparison through the browser

When the user requests a Synaps benchmark on a specified plan, agents may use
the normal website interface in the user's authorized browser session. This
includes automating the same UI steps a user would take to import the designated
plan, run tracing, and capture or download the result through the site's controls.
Use plans the user has authorized for submission to Synaps.

A restriction on direct private API calls does not prohibit normal browser UI
automation. Use the website controls for this workflow and respect access checks.
If a specific applicable restriction or technical error prevents a browser step,
identify its exact source or error and continue the available comparison work.

Treat Synaps output as a comparison result. Use the reviewed reference drawing
or IFC as ground truth when scoring both systems.

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
