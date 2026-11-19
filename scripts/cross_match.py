"""Cross-folder image <-> IFC storey matching (for mis-filed folders) + review page.

Images were sometimes filed in the wrong project folder, so every image is
registered against the storeys of its own folder AND the neighbouring folders
(--window 1 = N-1..N+1). One image may belong to several storeys (a sheet with
two plans), so nothing is assigned 1:1 here: each image gets its top
candidates, and you decide on the page.

  .venv\\Scripts\\python.exe scripts\\cross_match.py scan  [--window 1] [--folders 7 8 9] [--workers 4]
  .venv\\Scripts\\python.exe scripts\\cross_match.py page          # -> dataset\\gt\\cross\\index.html

Open index.html, tick the right storeys (or add one from the list), click
"download matches.txt". Scan results are cached per folder (cross/scan/*.json);
delete a file there to redo that folder.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trace_engine import paths  # noqa: E402

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
TOP = 4


def folder_no(name: str) -> int:
    m = re.match(r"\s*(\d+)", name)
    return int(m.group(1)) if m else -1


def natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def project_folders(root: Path):
    return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: natural(p.name))


def images_in(folder: Path):
    return [p for p in sorted(folder.iterdir(), key=lambda p: natural(p.name))
            if p.is_file() and p.suffix.lower() in IMG_EXT and not p.name.startswith(("preview_", "crop_", "overlay"))]


def sid(folder: str, key: str) -> str:
    return f"{folder}|{key}"


# ------------------------------------------------------------------ scan
def scan_folder(args) -> dict:
    folder, pool_folders, out_json = args
    from trace_engine.ifc_groundtruth import storeys_for_folder
    from trace_engine.register import IMAGE_FRAME_POSES, MAX_LINE_DENSITY, load_image, prepare_model, register

    t0 = time.time()
    pool = []  # (folder name, gt, model)
    for pf in pool_folders:
        try:
            for gt in storeys_for_folder(pf):
                pool.append((Path(pf).name, gt, prepare_model(gt)))
        except Exception as exc:  # a broken IFC must not stop the scan
            print(f"  {Path(pf).name}: IFC error {type(exc).__name__}: {exc}", flush=True)
    res = {"folder": Path(folder).name, "pool": [Path(p).name for p in pool_folders], "images": {}}
    for img in images_in(Path(folder)):
        try:
            ev = load_image(img)
            dense = float(ev.lines.mean()) > MAX_LINE_DENSITY
            cands = []
            if not dense:
                for fn, gt, model in pool:
                    reg = register(ev, gt, model, top=2, poses=IMAGE_FRAME_POSES)
                    if reg is not None:
                        cands.append({"folder": fn, "storey": gt.key, "match": round(reg.match, 3),
                                      "score": round(reg.score, 3), "support": round(reg.support, 3)})
            cands.sort(key=lambda c: -c["match"])
            res["images"][img.name] = {"render": dense, "top": cands[:TOP]}
            best = cands[0] if cands else None
            print(f"  {Path(folder).name}/{img.name[:10]}: " + ("render/photo" if dense else
                  f"best {best['folder'][:8]} {best['storey']} match {best['match']:.2f}" if best else "no walls"),
                  flush=True)
        except Exception as exc:
            res["images"][img.name] = {"error": f"{type(exc).__name__}: {exc}", "top": []}
    res["seconds"] = round(time.time() - t0, 1)
    Path(out_json).write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def cmd_scan(a) -> int:
    root = Path(a.root)
    out = Path(a.out) / "scan"
    out.mkdir(parents=True, exist_ok=True)
    folders = project_folders(root)
    todo = []
    for f in folders:
        n = folder_no(f.name)
        if a.folders and n not in set(int(x) for x in a.folders):
            continue
        if not images_in(f):
            continue
        target = out / f"{f.name}.json"
        if target.exists() and not a.redo:
            print(f"skip {f.name} (cached)")
            continue
        pool = [p for p in folders if abs(folder_no(p.name) - n) <= a.window and list(p.rglob("*.ifc"))]
        todo.append((str(f), [str(p) for p in pool], str(target)))
    print(f"{len(todo)} folder(s) to scan, window {a.window}")
    with ProcessPoolExecutor(max_workers=max(1, a.workers)) as ex:
        for fut in as_completed([ex.submit(scan_folder, t) for t in todo]):
            r = fut.result()
            print(f"done {r['folder']} in {r['seconds']} s")
    print("next: scripts\\cross_match.py page")
    return 0


# ------------------------------------------------------------------ page
def storey_thumb(gt, out: Path, side: int = 300) -> None:
    from PIL import Image, ImageDraw

    pts = [p for _, poly in gt.wall_polygons for p in poly]
    if not pts:
        return
    x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
    x1, y1 = max(p[0] for p in pts), max(p[1] for p in pts)
    k = (side - 16) / max(x1 - x0, y1 - y0, 1.0)
    w, h = int((x1 - x0) * k) + 16, int((y1 - y0) * k) + 16
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    tp = lambda p: (8 + (p[0] - x0) * k, 8 + (y1 - p[1]) * k)  # noqa: E731  (y up -> down)
    for kind, poly in gt.wall_polygons:
        d.polygon([tp(p) for p in poly], fill=(40, 40, 40) if kind == "wall" else (230, 140, 0))
    for o in gt.openings:
        d.polygon([tp(p) for p in o.polygon], fill=(30, 130, 255) if o.kind == "window" else (30, 190, 60))
    im.save(out, quality=82)


def thumb_img(src: Path, out: Path, side: int = 420) -> None:
    from PIL import Image, ImageOps

    if out.exists():
        return
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    im.thumbnail((side, side))
    im.save(out, quality=80)


def cmd_page(a) -> int:
    from trace_engine.ifc_groundtruth import storeys_for_folder

    root, base = Path(a.root), Path(a.out)
    (base / "t").mkdir(parents=True, exist_ok=True)
    folders = project_folders(root)
    storeys = []  # {id, label, thumb}
    sthumb = {}
    for fi, f in enumerate(folders):
        try:
            gts = storeys_for_folder(f)
        except Exception as exc:
            print(f"{f.name}: IFC error {exc}")
            continue
        for gt in gts:
            tn = f"s{fi}_{re.sub(r'[^A-Za-z0-9]+', '_', gt.key)}.jpg"
            if not (base / "t" / tn).exists():
                storey_thumb(gt, base / "t" / tn)
            i = sid(f.name, gt.key)
            sthumb[i] = f"t/{tn}"
            storeys.append({"id": i, "label": f"{f.name} | {gt.key}", "thumb": sthumb[i],
                            "folder": f.name, "walls": int(gt.stats.get("walls", 0))})
        print(f"{f.name}: {len(gts)} storeys")

    scans = {}
    for p in (base / "scan").glob("*.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        scans[d["folder"]] = d

    parts = [PAGE_HEAD]
    n = 0
    for fi, f in enumerate(folders):
        imgs = images_in(f)
        if not imgs:
            continue
        sc = scans.get(f.name, {}).get("images", {})
        parts.append(f"<h2>{html.escape(f.name)}"
                     + ("" if f.name in scans else " <small>(not scanned - run scan)</small>") + "</h2><div class=grid>")
        for img in imgs:
            tn = f"i{fi}_{re.sub(r'[^A-Za-z0-9]+', '_', img.stem)}.jpg"
            thumb_img(img, base / "t" / tn)
            info = sc.get(img.name, {})
            tops = info.get("top", [])
            cand = "".join(
                f"<label class=cand><input type=checkbox value='{html.escape(sid(c['folder'], c['storey']))}' "
                f"onchange=pick(this)><img loading=lazy src='{sthumb.get(sid(c['folder'], c['storey']), '')}' "
                f"onclick='zoom(event,this)'><span class='m {'hi' if c['match'] >= 0.3 and c['score'] >= 0.2 else ''}'>"
                f"{html.escape(c['folder'][:12])} {html.escape(c['storey'])}<br>match {c['match']:.2f} "
                f"score {c['score']:.2f} sup {c['support']:.2f}</span></label>" for c in tops)
            note = " &middot; <b>render/photo</b>" if info.get("render") else ""
            parts.append(
                f"<div class=card data-folder='{html.escape(f.name)}' data-img='{html.escape(img.name)}'>"
                f"<div class=src><img src='t/{tn}' onclick='zoom(event,this)'>"
                f"<div class=n>{html.escape(img.name)}{note}</div>"
                f"<select onchange=addExtra(this)><option value=''>+ add any other storey...</option>"
                f"{{OPTS}}</select><div class=extra></div></div><div class=cands>{cand}</div></div>")
            n += 1
        parts.append("</div>")
    opts, cur = [], None
    for s in storeys:
        if s["folder"] != cur:
            if cur is not None:
                opts.append("</optgroup>")
            opts.append(f"<optgroup label='{html.escape(s['folder'])}'>")
            cur = s["folder"]
        opts.append(f"<option value='{html.escape(s['id'])}'>{html.escape(s['label'].split(' | ')[1])} "
                    f"({s['walls']} walls)</option>")
    opts.append("</optgroup>")
    body = "".join(parts).replace("{OPTS}", "".join(opts))
    thumbs = json.dumps({s["id"]: s["thumb"] for s in storeys})
    (base / "index.html").write_text(body + PAGE_TAIL.replace("__THUMBS__", thumbs), encoding="utf-8")
    print(f"{n} images -> {base / 'index.html'}")
    return 0


PAGE_HEAD = """<!doctype html><meta charset=utf-8><title>Cross-folder matching</title>
<style>
body{font:13px system-ui;margin:0;background:#f2f2f2}
header{position:sticky;top:0;z-index:5;background:#222;color:#fff;padding:8px 14px;display:flex;gap:14px;align-items:center}
button{padding:6px 12px;font-size:14px;cursor:pointer}h2{margin:16px 14px 4px}
.grid{margin:6px 14px 24px;display:flex;flex-direction:column;gap:8px}
.card{display:flex;gap:10px;background:#fff;border:1px solid #ccc;padding:8px}.card.set{border-color:#2a9d3f;box-shadow:0 0 0 2px #2a9d3f55}
.src{width:300px;flex:none}.src>img{width:300px;height:260px;object-fit:contain;background:#fafafa;cursor:zoom-in}
.n{font-size:11px;color:#666;word-break:break-all}.src select{width:100%;margin-top:4px}
.cands{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-start}
.cand{width:210px;border:2px solid #ddd;padding:4px;cursor:pointer;display:block}.cand:has(input:checked){border-color:#2a9d3f;background:#eaf7ed}
.cand img{width:100%;height:180px;object-fit:contain;background:#fff}.m{font-size:11px;color:#555;display:block}.m.hi{color:#0a6;font-weight:600}
.extra .cand{display:inline-block;width:150px}.extra img{height:120px}
#big{position:fixed;inset:0;background:#000d;display:none;z-index:9;align-items:center;justify-content:center}
#big img{max-width:96vw;max-height:96vh;background:#fff}
</style>
<header><b>Tick every storey that is drawn in the image</b><span id=cnt></span>
<button onclick=dl()>download matches.txt</button><span style="opacity:.7">saved in this browser; click thumbnails to enlarge. Dark = walls, green = doors, blue = windows</span></header>
<div id=big onclick="this.style.display='none'"><img></div>
"""

PAGE_TAIL = """<script>
const T=__THUMBS__;const K='crossmatch.v1';let S={};try{S=JSON.parse(localStorage.getItem(K)||'{}')}catch(e){}
const key=c=>c.dataset.folder+'|'+c.dataset.img;
function zoom(e,i){e.preventDefault();const b=document.getElementById('big');b.firstChild.src=i.src;b.style.display='flex'}
function persist(){try{localStorage.setItem(K,JSON.stringify(S))}catch(e){}
 document.querySelectorAll('.card').forEach(c=>c.classList.toggle('set',(S[key(c)]||[]).length>0));
 document.getElementById('cnt').textContent=Object.values(S).reduce((a,v)=>a+v.length,0)+' matches on '+Object.values(S).filter(v=>v.length).length+' images'}
function setv(c,id,on){const k=key(c);let L=S[k]||[];L=L.filter(x=>x!==id);if(on)L.push(id);S[k]=L;persist()}
function pick(cb){setv(cb.closest('.card'),cb.value,cb.checked)}
function chip(c,id){const ex=c.querySelector('.extra');if(ex.querySelector('[data-id="'+CSS.escape(id)+'"]'))return;
 const l=document.createElement('label');l.className='cand';l.dataset.id=id;l.innerHTML='<input type=checkbox checked><img src="'+T[id]+'"><span class=m>'+id.replace('|',' | ')+' (click to remove)</span>';
 l.querySelector('input').onchange=e=>{setv(c,id,e.target.checked);if(!e.target.checked)l.remove()};ex.appendChild(l)}
function addExtra(sel){const c=sel.closest('.card');if(!sel.value)return;setv(c,sel.value,true);
 const cb=[...c.querySelectorAll('.cands input')].find(x=>x.value===sel.value);if(cb)cb.checked=true;else chip(c,sel.value);sel.value=''}
document.querySelectorAll('.card').forEach(c=>{(S[key(c)]||[]).forEach(id=>{const cb=[...c.querySelectorAll('.cands input')].find(x=>x.value===id);if(cb)cb.checked=true;else if(T[id])chip(c,id)})});persist();
function dl(){const L=[];document.querySelectorAll('.card').forEach(c=>(S[key(c)]||[]).forEach(id=>L.push(c.dataset.folder+'\\t'+c.dataset.img+'\\t'+id.replace('|','\\t'))));
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([L.join('\\n')+'\\n'],{type:'text/plain'}));a.download='matches.txt';a.click()}
</script>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(paths.vector_exports_root()))
    ap.add_argument("--out", default=str(paths.gt_root() / "cross"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--window", type=int, default=1, help="also search IFCs of folders N-window..N+window")
    s.add_argument("--folders", nargs="*", help="folder numbers to scan (default: all)")
    s.add_argument("--workers", type=int, default=4)
    s.add_argument("--redo", action="store_true")
    s.set_defaults(func=cmd_scan)
    p = sub.add_parser("page")
    p.set_defaults(func=cmd_page)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
