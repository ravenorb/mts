# Manufacturing Tracking System (MTS)

Web-based manufacturing tracking system with relational schema, CRUD UI, authentication/RBAC, pallet workflow, revision tracking, and traveler generation.

## Tech Stack
- FastAPI + Jinja2 web UI
- SQLAlchemy ORM
- SQLite (default, mapped as Docker volume)
- Docker / Docker Compose

## Included Functional Areas
- Items/parts, part revisions, process definitions
- BOMs
- Cut sheets + revision tracking + outputs
- Stations and queues
- Maintenance tasks and maintenance requests
- Consumables, purchase requests, usage logs
- Employees, skills, employee skill matrix
- Pallets + pallet revisions + pallet parts + pallet events
- Manual pallet create/edit
- Pallet split and combine actions
- Dashboard with plant status metrics
- Traveler file generated for each pallet change (`/data/pdfs/traveler_<pallet>.txt`)

## Data Paths (externally mappable)
Docker paths (volume-mapped in compose):
- SQL DB: `/data/sql/mts.db`
- Drawings: `/data/drawings`
- PDFs/Travelers: `/data/pdfs`
- Part revision files: `/data/part_revision_files`
- Runtime settings (branch/path config): `/data/config/runtime_settings.json`

Host paths:
- `./data/sql`
- `./data/drawings`
- `./data/pdfs`
- `./data/part_revision_files`
- `./data/config`

## Run
```bash
docker compose up --build
```
Portal: `http://localhost/` (port 80)

Default login:
- username: `admin`
- password: `admin123`

## Schema
- SQL DDL: `schema.sql`
- Runtime ORM schema: `app/models.py`
