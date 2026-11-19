"""Hand-matching page: plan images next to the exporter's floor PDFs, per folder.

  .venv\\Scripts\\python.exe scripts\\make_match_sheet.py [--folders "1 FIRST 6" ...]

Writes dataset/gt/match/index.html (+ thumbnails). Open it, pick the floor for
each image (or leave "none"), click "download matches.txt". Needs Pillow and
pdftoppm (poppler) or PyMuPDF for the PDFs.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from PIL import Image  # noqa: E402

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
THUMB = 520


def render_pdf(pdf: Path, out: Path) -> bool:
    if out.exists():
        return True
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:  # scratch outside the data folder (no deletes needed there)
        png = Path(tmp) / "p.png"
        if shutil.which("pdftoppm"):
            r = subprocess.run(["pdftoppm", "-png", "-r", "60", "-singlefile", str(pdf), str(png.with_suffix(""))],
                               capture_output=True)
            if r.returncode == 0 and png.exists():
                Image.open(png).convert("RGB").save(out, quality=80)
                return True
        try:
            import fitz  # type: ignore

            fitz.open(pdf)[0].get_pixmap(dpi=60).save(str(png))
            Image.open(png).convert("RGB").save(out, quality=80)
            return True
        except Exception:
            return False


def crop_white(im: Image.Image) -> Image.Image:
    from PIL import ImageChops

    bg = Image.new(im.mode, im.size, (255, 255, 255))
    box = ImageChops.difference(im, bg).convert("L").point(lambda v: 255 if v > 12 else 0).getbbox()
    return im.crop(box) if box else im


def thumb(src: Path, out: Path, crop: bool = False) -> None:
    if out.exists():
        return
    im = Image.open(src).convert("RGB")
    if crop:
        im = crop_white(im)
    im.thumbnail((THUMB, THUMB))
    im.save(out, quality=80)


def natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def main() -> int:
    import os

    data = Path(os.environ.get("TOPOSPATIAL_DATA") or ROOT / "dataset")  # same rule as trace_engine.paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(data / "raw_vector_exports" / "PDF plan"))
    ap.add_argument("--folders", nargs="*")
    ap.add_argument("--out", default=str(data / "gt" / "match"))
    args = ap.parse_args()
    root, out = Path(args.root), Path(args.out)
    (out / "t").mkdir(parents=True, exist_ok=True)
    report = {}
    rp = data / "gt" / "build_report.json"
    if rp.is_file():
        for f in json.loads(rp.read_text(encoding="utf-8")):
            report[f["folder"]] = {s["image"]: (s["storey"], s["bucket"]) for s in f["samples"]}

    folders = [p for p in sorted(root.iterdir(), key=lambda p: natural(p.name)) if p.is_dir()]
    if args.folders:
        folders = [p for p in folders if p.name in set(args.folders)]

    sections, n_img = [], 0
    for fi, fol in enumerate(folders):
        floors = []
        for pdf in sorted((fol / "unpacked_floors").glob("*.pdf"), key=lambda p: natural(p.name)) \
                if (fol / "unpacked_floors").is_dir() else []:
            name = pdf.stem
            tp = out / "t" / f"f{fi}_{re.sub(r'[^A-Za-z0-9]+', '_', name)}.jpg"
            if render_pdf(pdf, tp):
                im = crop_white(Image.open(tp).convert("RGB"))
                im.thumbnail((THUMB, THUMB))
                im.save(tp, quality=80)
                floors.append((name, tp.name))
        if not floors:
            continue
        cards = []
        for img in sorted((p for p in fol.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXT
                           and not p.name.startswith(("preview_", "crop_", "overlay"))),
                          key=lambda p: natural(p.name)):
            tp = out / "t" / f"i{fi}_{re.sub(r'[^A-Za-z0-9]+', '_', img.stem)}.jpg"
            thumb(img, tp)
            auto = report.get(fol.name, {}).get(img.name)
            cards.append((img.name, tp.name, auto))
            n_img += 1
        sections.append((fol.name, floors, cards))
        print(f"{fol.name}: {len(floors)} floor PDFs, {len(cards)} images")

    parts = ["""<!doctype html><meta charset=utf-8><title>Match images to floors</title>
<style>
body{font:14px system-ui;margin:0;background:#f4f4f4}
header{position:sticky;top:0;z-index:5;background:#222;color:#fff;padding:8px 14px;display:flex;gap:14px;align-items:center}
button{padding:6px 12px;font-size:14px;cursor:pointer}
h2{margin:18px 14px 4px}
.floors{display:flex;flex-wrap:wrap;gap:8px;margin:6px 14px;padding:8px;background:#fff;border:1px solid #ccc}
.f{width:150px;text-align:center;font-size:12px}.f img{width:150px;height:150px;object-fit:contain;background:#fff;border:1px solid #ddd}
.imgs{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;margin:6px 14px 20px}
.c{background:#fff;border:1px solid #ccc;padding:6px}.c.set{border-color:#2a9d3f;box-shadow:0 0 0 2px #2a9d3f55}
.c img{width:100%;height:240px;object-fit:contain;background:#fafafa;cursor:zoom-in}
.n{font-size:11px;color:#666;word-break:break-all}select{width:100%;font-size:14px;padding:4px;margin-top:4px}
#big{position:fixed;inset:0;background:#000d;display:none;z-index:9;align-items:center;justify-content:center}
#big img{max-width:96vw;max-height:96vh;background:#fff}
</style>
<header><b>Match each image to its floor</b><span id=cnt></span>
<button onclick=dl()>download matches.txt</button><span style="opacity:.7">choices are saved in this browser; click any thumbnail to enlarge</span></header>
<div id=big onclick="this.style.display='none'"><img></div>
"""]
    for si, (name, floors, cards) in enumerate(sections):
        parts.append(f"<h2>{html.escape(name)}</h2><div class=floors>")
        for fl, tn in floors:
            parts.append(f"<div class=f><img src='t/{tn}' onclick=zoom(this)><br>{html.escape(fl)}</div>")
        parts.append("</div><div class=imgs>")
        opts = "<option value=''>none / not a plan of this IFC</option>" + "".join(
            f"<option value='{html.escape(fl)}'>{html.escape(fl)}</option>" for fl, _ in floors)
        for img, tn, auto in cards:
            hint = f" &middot; auto guess: {html.escape(auto[0])} ({auto[1]})" if auto else ""
            parts.append(f"<div class=c data-folder='{html.escape(name)}' data-img='{html.escape(img)}'>"
                         f"<img src='t/{tn}' onclick=zoom(this)><div class=n>{html.escape(img)}{hint}</div>"
                         f"<select onchange=save(this)>{opts}</select></div>")
        parts.append("</div>")
    parts.append("""<script>
const K='matchsheet.v1';let S={};try{S=JSON.parse(localStorage.getItem(K)||'{}')}catch(e){}
function key(c){return c.dataset.folder+'|'+c.dataset.img}
function zoom(i){const b=document.getElementById('big');b.firstChild.src=i.src;b.style.display='flex'}
function count(){document.getElementById('cnt').textContent=Object.values(S).filter(v=>v).length+' matched'}
function save(sel){const c=sel.closest('.c');S[key(c)]=sel.value;c.classList.toggle('set',!!sel.value);
 try{localStorage.setItem(K,JSON.stringify(S))}catch(e){};count()}
document.querySelectorAll('.c').forEach(c=>{const v=S[key(c)];if(v){c.querySelector('select').value=v;c.classList.add('set')}});count();
function dl(){const L=[];document.querySelectorAll('.c').forEach(c=>{const v=S[key(c)];if(v)L.push(c.dataset.folder+'\\t'+c.dataset.img+'\\t'+v)});
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([L.join('\\n')+'\\n'],{type:'text/plain'}));a.download='matches.txt';a.click()}
</script>""")
    (out / "index.html").write_text("".join(parts), encoding="utf-8")
    print(f"{n_img} images in {len(sections)} folders -> {out / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
