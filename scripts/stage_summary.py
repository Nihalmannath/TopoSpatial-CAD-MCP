"""Table of stage counts for every run.json under a folder (written by stage_trace.py).

    .venv\\Scripts\\python.exe scripts\\stage_summary.py scratch\\repro_audit
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

COLUMNS = [("mask_wall_px", "mask px"), ("opened_wall_px", "opened"), ("wall_thickness_px", "t px"),
           ("runs_extracted", "runs"), ("runs_bridged", "bridged"), ("separators", "seps"),
           ("doors_found", "doors"), ("windows_found", "wins"), ("compiled_wall", "C.walls"),
           ("compiled_room", "C.rooms"), ("compiled_door", "C.doors"), ("compiled_window", "C.wins")]


def rows(folder: Path):
    for f in sorted(folder.glob("*/run.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        yield f.parent.name, r


def table(folder: Path) -> str:
    head = f"{'run':44} {'sha':12} {'bias':>4} " + " ".join(f"{h:>8}" for _, h in COLUMNS) + "  note"
    lines = [head, "-" * len(head)]
    for name, r in rows(folder):
        c = r.get("counts", {})
        note = r.get("early_exit") or r.get("compile_error") or ""
        bias = r.get("wall_logit_bias")
        lines.append(f"{name[:44]:44} {str(r.get('weights_sha256'))[:12]:12} {'' if bias is None else f'{bias:+.0f}':>4} "
                     + " ".join(f"{c.get(k, ''):>8}" for k, _ in COLUMNS) + f"  {note}")
    return "\n".join(lines)


if __name__ == "__main__":
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "scratch/repro_audit")
    text = table(folder)
    (folder / "summary.txt").write_text(text, encoding="utf-8")
    print(text)
