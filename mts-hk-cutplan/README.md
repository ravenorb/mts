# HK MPF Cut Planner (MTS module)

Server-rendered MTS-style module for HK/Siemens MPF files:
- Parse HKOST/HKSTR/HKCUT/HKSTO blocks into parts/contours/segments
- Visualize in canvas with contour order labels
- Reorder contour blocks and export reordered MPF
- Compute skeleton region (sheet - parts) and generate "skeleton breakup" MPF:
  - 3 horizontal lines (evenly spaced by Y)
  - 2 vertical lines (evenly spaced by X)
  - clipped to cut skeleton only (never through parts)

## Setup
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Open:

http://localhost:8000/cutplan

Environment

MTS_DATA_DIR (default: ./data) for mpf uploads and generated outputs

MTS_DB_PATH (optional) override sqlite path (default: ./data/mts.db)

---

# What Codex should implement next (clean TODO list)

Once this is in, you hand Codex this list and it’ll do the rest of your job (as usual):

1. Add `GET /api/cutplan/{job_id}/latest_model`  
   - returns skeleton artifact JSON if present, else parsed
2. Improve arc rendering in UI (polyline is already fine, but we can show true arcs later)
3. Replace “contour-block reorder” with **HKOST part-block reorder** (safer for real jobs)
4. Shared/common edge detection (optional, but you asked for it)
5. Allow isolating a single part via clicking actual geometry (hit test) instead of cycling

---

# Notes you’ll care about

- **Units:** everything uses inches because you said so.
- **Skeleton cuts:** the clipping is done server-side with Shapely:
  - it literally cannot emit segments inside parts unless geometry is broken
- **HK program semantics:** the skeleton MPF append is intentionally minimal; if HK requires special header/tech codes, Codex can wrap it in your standard HK macro sequence.

If you want the skeleton MPF to be inserted as a specific `HKOST(..., <existing tech>, ...)` instead of `tech=99`, tell me what tech code you want and I’ll hardwire it.
