# MTS Current Architecture Extraction

## 1) High-level system map

## Runtime structure
- **Application style:** Single FastAPI application (`app/main.py`) that contains HTTP routes, data-access, business rules, startup migrations, parsing helpers, and file generation in one module.
- **Persistence:** SQLAlchemy models in `app/models.py`, session/engine wiring in `app/database.py`, SQLite default data path with runtime path override support.
- **Presentation:** Jinja2 templates under `app/templates` with server-rendered UI.
- **Auth:** Session-based auth, with role checks and write permissions managed in-route.

## Major modules (as implemented today)
1. **Production / pallet execution**
   - Pallet creation, edit, move, release, delete.
   - Production order + pallet auto-creation behavior.
   - Station queue and pallet route progression.
2. **Engineering data + file intake**
   - Part master/revisions/BOM/header editing.
   - HK MPF/PDF parsing workflows.
   - Revision-file upload and station-scoped file availability.
3. **Stations / operator execution**
   - Station login gate and queue progression.
   - Start-next and complete-pallet workflows.
   - Component completion/scrap capture.
4. **Maintenance**
   - Scheduled + requested maintenance requests.
   - Task definitions per station.
   - Consumable usage and closure logs.
5. **Inventory + purchasing support**
   - Storage locations/bins.
   - Raw materials, consumables, scrap steel.
   - Part inventory and delivered part lots.
6. **Admin + generic CRUD**
   - Generic entity list/edit/save/delete over `MODEL_MAP`.
   - Server maintenance actions (branch switch/pull/path updates).

## Entry points
- `@app.on_event("startup")` performs schema creation plus live schema mutation and seed behavior.
- Main web entry routes:
  - `/` dashboard
  - `/production`
  - `/engineering`
  - `/stations`
  - `/maintenance`
  - `/inventory`
  - `/admin`
  - `/entity/{entity}` (generic data admin)

## Data flow (current)
1. **Request enters route handler** in `main.py`.
2. Route performs business logic directly (validation, status transitions, quantity arithmetic, inventory updates).
3. Route reads/writes ORM models directly.
4. Route may write files directly (traveler text, uploaded PDFs/program files).
5. Route returns template/redirect/JSON.

No dedicated domain/service layer boundary exists today; most workflows are route-owned.

## Core workflows (current)

### A) Pallet lifecycle (core path)
1. Engineering data establishes part/revision/BOM/MPF context.
2. Production creates order+pallet (manual or MPF-based), sets component snapshot and pallet parts.
3. Pallet staged in storage bin or released to first station queue.
4. Station starts pallet, marks work completion/scrap by component.
5. Pallet routed to next station queue, storage, split path, or completion.
6. Events and traveler file act as traceability artifacts.

### B) Traveler generation
- Traveler is generated as a text file in `PDF_DIR` for selected pallet mutations (`create`, `split`, `combine`, some entity save paths).
- Content is derived from current pallet and pallet parts only.

### C) Engineering intake workflow
- Uploading PDF (and optionally MPF) parses part + components, updates part master, revision header, BOM lines, optional MPF master/details, and engineering PDF catalog.

### D) Maintenance workflow
- Scheduler function auto-creates upcoming scheduled requests from tasks.
- Manual requests can be updated with status and consumable usage.
- Completion writes maintenance logs and updates next task due time.

---

## 2) Architecture findings (problem-focused)

## Route files containing business logic
- **Primary concentration:** `app/main.py` (effectively all business logic).
- Examples of route-heavy domain logic:
  - Pallet order math, BOM/component derivation, routing creation, inventory effects.
  - Station completion logic (partial completion, scrap, leftover split behavior, queue transitions).
  - Engineering parser post-processing and schema backfill behavior.

## Domain logic mixed with UI concerns
- Template selection + rendering and domain mutations are interleaved in same handlers.
- Handlers compute dashboard metrics inline alongside routing/auth concerns.
- File generation and upload persistence executed inside HTTP handlers.

## Duplicated logic
- Pallet deletion cleanup duplicated in `/production/pallet/{id}/delete` and generic `/entity/{entity}/{id}/delete` for `pallets`.
- Pallet split behavior exists in station completion (“spawn_leftover”) and standalone `/pallets/{id}/split`, with different semantics.
- Inventory mutation concerns are split across production release, delete rollback, and direct inventory edit routes.
- Component/part-revision ensuring logic appears in multiple workflows.

## Unclear ownership between layers
- Models are mostly schema-only, but business behavior is not centralized; instead routes call many helper functions in same file.
- “Service-like” helpers exist (inventory updates, traveler generation, parsing, routing) but remain coupled to HTTP module.
- Startup routine mutates database schema, effectively mixing migration responsibilities with application startup.

## True core domain of MTS
The true core domain is:

> **Pallet lifecycle execution and traceability across stations, from engineered definition to production completion.**

Supporting domains:
- Traveler/traceability artifact generation.
- Engineering-to-production handoff (MPF/PDF/BOM extraction).
- Inventory synchronization to execution events.

Operationally, the system heartbeat is still “where each pallet is, what has been completed, what is queued next, and what evidence was recorded.”

---

## 3) Structural stability observations

## Strengths
- Single runtime has coherent end-to-end manufacturing workflow coverage.
- Data model supports revisions, queueing, station logs, and event timeline.
- Existing helper functions provide extraction anchors for future layering.

## Stability risks
- Single large module increases regression risk for any change.
- Route-level transaction and rule handling can drift (same concept handled differently across endpoints).
- Startup-time schema mutations hide migration state and can surprise deployments.
- Generic CRUD allows broad writes to production-critical tables without domain guardrails.

