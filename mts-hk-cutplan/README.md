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
