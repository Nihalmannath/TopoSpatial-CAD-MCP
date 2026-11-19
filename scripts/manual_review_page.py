"""Review page for `build_gt_dataset.py manual`: image | traced IFC overlay | IFC plan, side by side.

  python scripts/manual_review_page.py --gt dataset/gt [--root <PDF plan folder>]

Reads <gt>/manual_report.json and <gt>/candidates/<id>/overlay.png, writes
<gt>/manual_review/index.html (+ thumbnails). Tick = correct pair; "download
reviewed.txt" lists the ticked sample ids for `accept --list`. Verified pairs
start ticked, weak ones unticked. Rows whose IFC lives in another folder than
the image are marked MOVED (image is filed in the wrong project folder).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from PIL import Image, ImageOps  # noqa: E402

from trace_engine import paths  # noqa: E402


def thumb(src: Path, out: Path, side: int = 640) -> None:
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    im.thumbnail((side, side))
    im.save(out, quality=78)


def main() -> int:
    from cross_match import storey_thumb
    from trace_engine.ifc_groundtruth import storeys_for_folder

    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", default=str(paths.gt_root()))
    ap.add_argument("--root", default=str(paths.vector_exports_root()))
    a = ap.parse_args()
    gt_dir, root = Path(a.gt), Path(a.root)
    out = gt_dir / "manual_review"
    (out / "t").mkdir(parents=True, exist_ok=True)
    report = [r for r in json.loads((gt_dir / "manual_report.json").read_text(encoding="utf-8")) if r.get("id")]
    cache = {}

    def storey(folder: str, key: str):
        if folder not in cache:
            cache[folder] = {g.key: g for g in storeys_for_folder(root / folder)}
        return cache[folder].get(key)

    order = {"manual_weak": 0, "manual_ok": 1}
    report.sort(key=lambda r: (order.get(r["status"], 2), r["image_folder"], r["image"]))
    cards = []
    for i, r in enumerate(report):
        cand = gt_dir / "candidates" / r["id"]
        src = root / r["image_folder"] / r["image"]
        t_img, t_ov, t_st = f"i{i}.jpg", f"o{i}.jpg", f"s{i}.png"
        try:
            thumb(src, out / "t" / t_img)
            thumb(cand / "overlay.png", out / "t" / t_ov)
            g = storey(r["ifc_folder"], r["storey"])
            if g is not None:
                storey_thumb(g, out / "t" / t_st, side=640)
        except Exception as exc:
            print("skip", r["id"], exc)
            continue
        ok = r["status"] == "manual_ok"
        moved = r.get("moved")
        cards.append(
            f"<div class='card {'ok' if ok else 'weak'}' data-id='{html.escape(r['id'])}'>"
            f"<div class=h><label><input type=checkbox {'checked' if ok else ''} onchange=upd()> <b>correct</b></label> "
            f"<span class=t>{html.escape(r['image_folder'])} / {html.escape(r['image'][:16])}&hellip;</span> "
            f"&rarr; <b>{html.escape(r['ifc_folder'])}</b> {html.escape(r['storey'])} "
            f"{'<span class=mv>MOVED: image is in the wrong folder</span>' if moved else ''} "
            f"<span class=m>{r['status'].replace('manual_', '')} &middot; match {r['match']:.2f} score {r['score']:.2f} "
            f"support {r['support']:.2f} &middot; {r['how']}</span></div>"
            f"<div class=row><figure><img loading=lazy src='t/{t_img}' onclick=zoom(this)><figcaption>image</figcaption></figure>"
            f"<figure><img loading=lazy src='t/{t_ov}' onclick=zoom(this)><figcaption>IFC traced over image "
            f"(red walls, blue windows, green doors)</figcaption></figure>"
            f"<figure><img loading=lazy src='t/{t_st}' onclick=zoom(this)><figcaption>IFC plan alone</figcaption></figure></div></div>")
    page = f"""<!doctype html><meta charset=utf-8><title>Manual match review</title>
<style>
body{{font:13px system-ui;margin:0;background:#f2f2f2}}
header{{position:sticky;top:0;z-index:5;background:#222;color:#fff;padding:8px 14px;display:flex;gap:14px;align-items:center}}
button{{padding:6px 12px;font-size:14px;cursor:pointer}}
.card{{background:#fff;border:1px solid #ccc;margin:8px 14px;padding:6px 8px}}.card.weak{{border-left:6px solid #d98200}}.card.ok{{border-left:6px solid #2a9d3f}}
.card:has(input:not(:checked)){{opacity:.6}}
.h{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:4px}}.t{{color:#444}}.m{{color:#666;font-size:12px}}
.mv{{background:#fde8c8;color:#8a4b00;padding:1px 6px;border-radius:3px;font-size:12px}}
.row{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}figure{{margin:0;text-align:center}}
figure img{{width:100%;height:420px;object-fit:contain;background:#fafafa;cursor:zoom-in}}figcaption{{font-size:11px;color:#666}}
#big{{position:fixed;inset:0;background:#000d;display:none;z-index:9;align-items:center;justify-content:center}}#big img{{max-width:96vw;max-height:96vh;background:#fff}}
</style>
<header><b>Is the middle picture (IFC over the image) correctly aligned?</b><span id=cnt></span>
<button onclick=dl()>download reviewed.txt</button><span style="opacity:.7">orange rows = weak fit, check first &middot; unticked rows are dropped</span></header>
<div id=big onclick="this.style.display='none'"><img></div>
{''.join(cards)}
<script>
const K='manualreview.v1';let S={{}};try{{S=JSON.parse(localStorage.getItem(K)||'{{}}')}}catch(e){{}}
const cs=[...document.querySelectorAll('.card')];
cs.forEach(c=>{{const v=S[c.dataset.id];if(v!==undefined)c.querySelector('input').checked=v}});
function zoom(i){{const b=document.getElementById('big');b.firstChild.src=i.src;b.style.display='flex'}}
function upd(){{cs.forEach(c=>S[c.dataset.id]=c.querySelector('input').checked);try{{localStorage.setItem(K,JSON.stringify(S))}}catch(e){{}}
 document.getElementById('cnt').textContent=cs.filter(c=>c.querySelector('input').checked).length+' / '+cs.length+' ticked'}}
upd();
function dl(){{const L=cs.filter(c=>c.querySelector('input').checked).map(c=>c.dataset.id);
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([L.join('\\n')+'\\n'],{{type:'text/plain'}}));a.download='reviewed.txt';a.click()}}
</script>"""
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"{len(cards)} pairs -> {out / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
