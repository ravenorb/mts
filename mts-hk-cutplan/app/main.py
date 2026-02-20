import os, re, json, math
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates

from shapely.geometry import Polygon, LineString, MultiLineString
from shapely.ops import unary_union

from .database import SessionLocal, engine
from .models import CutJob, CutArtifact
from .database import Base

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

# ------------------------
# Startup "migration"
# ------------------------
@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)

# ------------------------
# Auth stubs (adapt to your session)
# ------------------------
def require_role(request: Request, role: str):
    user = request.session.get("user") if hasattr(request, "session") else {"roles": ["read", "write"]}
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    roles = set(user.get("roles", []))
    if role not in roles:
        raise HTTPException(status_code=403, detail="Forbidden")

# ------------------------
# Storage
# ------------------------
def storage_root() -> Path:
    root = Path(os.environ.get("MTS_DATA_DIR", "data"))
    (root / "mpf").mkdir(parents=True, exist_ok=True)
    (root / "gen").mkdir(parents=True, exist_ok=True)
    return root

# ------------------------
# HK MPF parsing
# ------------------------
NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)"
RE_FLOATS = re.compile(NUM)
RE_X = re.compile(r"\bX(" + NUM + r")\b", re.I)
RE_Y = re.compile(r"\bY(" + NUM + r")\b", re.I)
RE_I = re.compile(r"\bI(" + NUM + r")\b", re.I)
RE_J = re.compile(r"\bJ(" + NUM + r")\b", re.I)

def _extract_call_floats(line: str, keyword: str) -> List[float]:
    m = re.search(rf"{keyword}\(([^)]*)\)", line, re.I)
    if not m:
        return []
    return [float(v) for v in RE_FLOATS.findall(m.group(1))]

def _arc_points(start, end, i, j, cw: bool, step_deg=6.0):
    sx, sy = start
    ex, ey = end
    cx, cy = sx + i, sy + j
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)

    if cw:
        while a1 > a0:
            a1 -= 2 * math.pi
    else:
        while a1 < a0:
            a1 += 2 * math.pi

    total = a1 - a0
    n = max(8, int(abs(total) / math.radians(step_deg)) + 1)
    r = math.hypot(sx - cx, sy - cy)

    pts = []
    for k in range(n + 1):
        t = k / n
        a = a0 + total * t
        pts.append([cx + r * math.cos(a), cy + r * math.sin(a)])
    return pts

def parse_hk_mpf(text: str) -> Dict[str, Any]:
    """
    Model:
      sheet: {width,height} (inches)
      parts: [{program_id, tech, contours:[{id,type,segments:...}]}]
    """
    x = y = 0.0
    cut_on = False

    sheet = {"width": None, "height": None}
    parts: List[Dict[str, Any]] = []
    current_part: Optional[Dict[str, Any]] = None
    current_contours: List[Dict[str, Any]] = []
    part_starts: Dict[int, List[Dict[str, Any]]] = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        u = line.upper()
        n_match = re.match(r"N(\d+)", u)
        line_no = int(n_match.group(1)) if n_match else None

        if u.startswith("HKINI"):
            vals = _extract_call_floats(u, "HKINI")
            if len(vals) >= 3:
                sheet["width"] = vals[1]
                sheet["height"] = vals[2]
            continue

        if "HKOST(" in u:
            vals = _extract_call_floats(u, "HKOST")
            program_id = int(vals[3]) if len(vals) >= 4 else None
            tech = int(vals[4]) if len(vals) >= 5 else None
            current_part = {
                "program_id": program_id,
                "tech": tech,
                "offset": [vals[0] if len(vals) >= 1 else 0.0, vals[1] if len(vals) >= 2 else 0.0],
                "contours": [],
            }
            parts.append(current_part)
            if program_id is not None:
                part_starts.setdefault(program_id, []).append(current_part)
            continue

        if "HKSTR(" in u:
            vals = _extract_call_floats(u, "HKSTR")
            # HKSTR args 3/4/5 are contour-local start coordinates (X/Y/Z).
            # HKOST provides sheet-level placement offsets keyed by HKSTR line.
            x = vals[2] if len(vals) >= 3 else x
            y = vals[3] if len(vals) >= 4 else y
            placements = part_starts.get(line_no or -1)
            if not placements:
                if current_part is None:
                    current_part = {"program_id": None, "tech": None, "offset": [0.0, 0.0], "contours": []}
                    parts.append(current_part)
                placements = [current_part]

            ctype = int(vals[0]) if len(vals) >= 1 else 0
            current_contours = []
            for placed_part in placements:
                contour = {
                    "type": "outer" if ctype == 0 else "hole",
                    "hkstr": vals,
                    "segments": [],
                }
                placed_part["contours"].append(contour)
                ox, oy = placed_part.get("offset", [0.0, 0.0])
                current_contours.append({"contour": contour, "offset": [ox, oy]})
            continue

        if "HKCUT" in u:
            cut_on = True
            continue
        if "HKSTO" in u:
            cut_on = False
            current_contours = []
            continue

        if "HKPED" in u:
            current_part = None
            current_contours = []
            cut_on = False
            continue

        if u.startswith("WHEN"):
            continue

        if not cut_on or not current_contours:
            continue

        if u.startswith("G1"):
            mx = RE_X.search(u)
            my = RE_Y.search(u)
            nx = float(mx.group(1)) if mx else x
            ny = float(my.group(1)) if my else y
            for active in current_contours:
                ox, oy = active["offset"]
                active["contour"]["segments"].append({"kind": "line", "a": [x + ox, y + oy], "b": [nx + ox, ny + oy]})
            x, y = nx, ny
            continue

        if u.startswith("G2") or u.startswith("G3"):
            mx = RE_X.search(u); my = RE_Y.search(u)
            mi = RE_I.search(u); mj = RE_J.search(u)
            if not (mx and my and mi and mj):
                continue
            nx, ny = float(mx.group(1)), float(my.group(1))
            i, j = float(mi.group(1)), float(mj.group(1))
            for active in current_contours:
                ox, oy = active["offset"]
                pts = _arc_points((x + ox, y + oy), (nx + ox, ny + oy), i, j, cw=u.startswith("G2"))
                active["contour"]["segments"].append({"kind": "polyline", "points": pts})
            x, y = nx, ny
            continue

    sheet["width"] = float(sheet["width"] or 0.0)
    sheet["height"] = float(sheet["height"] or 0.0)

    cid = 1
    for p in parts:
        p.pop("offset", None)
        for c in p["contours"]:
            c["id"] = cid
            cid += 1

    return {"sheet": sheet, "parts": parts}

# ------------------------
# Geometry helpers (Shapely)
# ------------------------
def _contour_to_ring(contour: Dict[str, Any], tol=1e-4) -> Optional[List[List[float]]]:
    pts: List[List[float]] = []
    for s in contour["segments"]:
        if s["kind"] == "line":
            if not pts:
                pts.append(s["a"])
            pts.append(s["b"])
        elif s["kind"] == "polyline":
            poly = s["points"]
            if not pts:
                pts.extend(poly)
            else:
                # avoid dup point at join
                if pts[-1] == poly[0]:
                    pts.extend(poly[1:])
                else:
                    pts.extend(poly)
    if len(pts) < 4:
        return None
    # ensure closed
    if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) > tol:
        pts.append(pts[0])
    return pts

def parts_to_polygons(model: Dict[str, Any]) -> List[Polygon]:
    """
    Build part polygons using:
      outer contours union
      minus holes contained within outer(s)
    """
    out: List[Polygon] = []
    for p in model["parts"]:
        outers: List[Polygon] = []
        holes: List[Polygon] = []

        for c in p["contours"]:
            ring = _contour_to_ring(c)
            if not ring:
                continue
            poly = Polygon(ring).buffer(0)
            if poly.is_empty:
                continue
            if c["type"] == "outer":
                outers.append(poly)
            else:
                holes.append(poly)

        if not outers:
            continue

        outer_union = unary_union(outers).buffer(0)

        # subtract holes that are actually inside
        hole_union = unary_union([h for h in holes if h.within(outer_union)]).buffer(0) if holes else None
        part_poly = outer_union.difference(hole_union).buffer(0) if hole_union else outer_union

        if not part_poly.is_empty:
            out.append(part_poly)

    return out

def compute_skeleton(model: Dict[str, Any]) -> Dict[str, Any]:
    w = model["sheet"]["width"]
    h = model["sheet"]["height"]
    sheet_poly = Polygon([(0,0),(w,0),(w,h),(0,h)]).buffer(0)

    part_polys = parts_to_polygons(model)
    parts_union = unary_union(part_polys).buffer(0) if part_polys else Polygon()

    skeleton = sheet_poly.difference(parts_union).buffer(0)

    # 3 horizontal at y = 1/4,2/4,3/4 and 2 vertical at x = 1/3,2/3
    ys = [h * (k/4.0) for k in (1,2,3)]
    xs = [w * (k/3.0) for k in (1,2)]

    candidates: List[LineString] = []
    for yy in ys:
        candidates.append(LineString([(0, yy), (w, yy)]))
    for xx in xs:
        candidates.append(LineString([(xx, 0), (xx, h)]))

    cut_lines: List[LineString] = []
    for ln in candidates:
        clipped = ln.intersection(skeleton)
        if clipped.is_empty:
            continue
        # belt-and-suspenders: ensure no part cutting
        clipped = clipped.difference(parts_union).buffer(0)
        if clipped.is_empty:
            continue
        if clipped.geom_type == "LineString":
            cut_lines.append(clipped)
        elif clipped.geom_type == "MultiLineString":
            cut_lines.extend([g for g in clipped.geoms if g.length > 1e-4])

    # serialize skeleton cuts as segments
    skel_cuts = []
    for i, ln in enumerate(cut_lines, start=1):
        coords = list(ln.coords)
        skel_cuts.append({"id": i, "a": [coords[0][0], coords[0][1]], "b": [coords[-1][0], coords[-1][1]]})

    model2 = dict(model)
    model2["skeletonCuts"] = skel_cuts
    return model2

# ------------------------
# MPF generation: skeleton cut "part"
# ------------------------
def generate_skeleton_mpf(original_text: str, model_with_skeleton: Dict[str, Any]) -> str:
    """
    Appends a new HKOST + HKSTR/HKCUT/HKSTO blocks for skeleton cuts.
    This is intentionally simple and deterministic.
    """
    lines = original_text.splitlines()

    # find a decent insertion spot: before final HKEND/M30 if present; else append
    insert_at = len(lines)
    for i, ln in enumerate(lines):
        u = ln.upper()
        if "HKEND" in u or u.strip().startswith("M30"):
            insert_at = i
            break

    # choose a program id unlikely to clash
    prog_id = 990001
    tech = 99

    out = []
    out.extend(lines[:insert_at])

    # new "part instance"
    out.append(f"N900000 HKOST(0.0,0.0,0.0,{prog_id},{tech},0,0,0)")

    n = 900010
    for cut in model_with_skeleton.get("skeletonCuts", []):
        ax, ay = cut["a"]
        bx, by = cut["b"]
        out.append(f"N{n} HKSTR(0,1,{ax:.4f},{ay:.4f},0,0,0,0)"); n += 10
        out.append("HKPIE(0,0,0)")
        out.append("HKLEA(0,0,0)")
        out.append("HKCUT(0,0,0)")
        out.append(f"G1 X{ax:.4f} Y{ay:.4f}")
        out.append(f"G1 X{bx:.4f} Y{by:.4f}")
        out.append("HKSTO(0,0,0)")

    out.append(f"N{n} HKPED(0,0,0)")
    out.extend(lines[insert_at:])
    return "\n".join(out)

# ------------------------
# Reorder export (contour-block reorder)
# ------------------------
def export_reordered_mpf(original_text: str, order: List[int]) -> str:
    lines = original_text.splitlines()
    blocks = []
    preamble = []
    postamble = []

    in_block = False
    cur = []
    seen_any = False

    for line in lines:
        u = line.strip().upper()
        if "HKSTR(" in u:
            seen_any = True
            in_block = True
            cur = [line]
            continue
        if in_block:
            cur.append(line)
            if "HKSTO" in u:
                blocks.append(cur)
                in_block = False
                cur = []
            continue
        if not seen_any:
            preamble.append(line)
        else:
            postamble.append(line)

    if len(order) != len(blocks):
        raise ValueError(f"order length {len(order)} != blocks {len(blocks)}")

    new_blocks = [blocks[cid - 1] for cid in order]

    out = []
    out.extend(preamble)
    for b in new_blocks:
        out.extend(b)
    out.extend(postamble)
    return "\n".join(out)

# ------------------------
# Routes + API
# ------------------------
@app.get("/cutplan")
def cutplan_index(request: Request):
    require_role(request, "read")
    with SessionLocal() as db:
        jobs = db.query(CutJob).order_by(CutJob.created_at.desc()).limit(50).all()
    return templates.TemplateResponse("cutplan/index.html", {"request": request, "jobs": jobs})

@app.post("/cutplan/upload")
async def cutplan_upload(request: Request, file: UploadFile = File(...), name: str = Form("MPF Job")):
    require_role(request, "write")
    root = storage_root()
    mpf_path = root / "mpf" / f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{file.filename}"
    content = await file.read()
    mpf_path.write_bytes(content)

    parsed = parse_hk_mpf(content.decode("utf-8", errors="ignore"))

    with SessionLocal() as db:
        job = CutJob(name=name, mpf_path=str(mpf_path))
        db.add(job)
        db.flush()
        db.add(CutArtifact(job_id=job.id, kind="parsed", json_text=json.dumps(parsed)))
        db.commit()

    return RedirectResponse(url=f"/cutplan/{job.id}", status_code=303)

@app.get("/cutplan/{job_id}")
def cutplan_view(request: Request, job_id: int):
    require_role(request, "read")
    with SessionLocal() as db:
        job = db.query(CutJob).filter(CutJob.id == job_id).first()
        if not job:
            raise HTTPException(404, "Job not found")
    return templates.TemplateResponse("cutplan/view.html", {"request": request, "job": job})

@app.get("/api/cutplan/{job_id}/model")
def api_cutplan_model(request: Request, job_id: int):
    require_role(request, "read")
    with SessionLocal() as db:
        art = (db.query(CutArtifact)
               .filter(CutArtifact.job_id == job_id, CutArtifact.kind == "parsed")
               .order_by(CutArtifact.created_at.desc()).first())
        if not art:
            raise HTTPException(404, "Parsed model not found")
        return JSONResponse(json.loads(art.json_text))

@app.post("/api/cutplan/{job_id}/compute_skeleton")
def api_compute_skeleton(request: Request, job_id: int):
    require_role(request, "write")
    root = storage_root()
    with SessionLocal() as db:
        job = db.query(CutJob).filter(CutJob.id == job_id).first()
        if not job:
            raise HTTPException(404, "Job not found")

        parsed_art = (db.query(CutArtifact)
                      .filter(CutArtifact.job_id == job_id, CutArtifact.kind == "parsed")
                      .order_by(CutArtifact.created_at.desc()).first())
        if not parsed_art:
            raise HTTPException(404, "Parsed model not found")

        model = json.loads(parsed_art.json_text)
        model2 = compute_skeleton(model)

        original = Path(job.mpf_path).read_text(encoding="utf-8", errors="ignore")
        skel_text = generate_skeleton_mpf(original, model2)
        out_path = root / "gen" / f"job_{job.id}_skeleton.mpf"
        out_path.write_text(skel_text, encoding="utf-8")

        db.add(CutArtifact(job_id=job.id, kind="skeleton", json_text=json.dumps(model2), file_path=str(out_path)))
        db.commit()

    return JSONResponse({"ok": True, "download": f"/cutplan/{job_id}/download/skeleton"})

@app.post("/api/cutplan/{job_id}/reorder")
async def api_cutplan_reorder(request: Request, job_id: int):
    require_role(request, "write")
    payload = await request.json()
    order = payload.get("order")
    if not isinstance(order, list) or not all(isinstance(x, int) for x in order):
        raise HTTPException(400, "order must be list[int]")

    root = storage_root()
    with SessionLocal() as db:
        job = db.query(CutJob).filter(CutJob.id == job_id).first()
        if not job:
            raise HTTPException(404, "Job not found")
        original = Path(job.mpf_path).read_text(encoding="utf-8", errors="ignore")

        new_text = export_reordered_mpf(original, order)
        out_path = root / "gen" / f"job_{job.id}_reordered.mpf"
        out_path.write_text(new_text, encoding="utf-8")

        db.add(CutArtifact(job_id=job.id, kind="reordered", file_path=str(out_path), json_text=json.dumps({"order": order})))
        db.commit()

    return JSONResponse({"ok": True, "download": f"/cutplan/{job_id}/download/reordered"})

@app.get("/cutplan/{job_id}/download/{kind}")
def cutplan_download(request: Request, job_id: int, kind: str):
    require_role(request, "read")
    if kind not in ("reordered", "skeleton"):
        raise HTTPException(400, "Invalid kind")
    with SessionLocal() as db:
        art = (db.query(CutArtifact)
               .filter(CutArtifact.job_id == job_id, CutArtifact.kind == kind)
               .order_by(CutArtifact.created_at.desc()).first())
        if not art or not art.file_path:
            raise HTTPException(404, "File not found")
        return FileResponse(art.file_path, filename=os.path.basename(art.file_path))
