# V2 Architecture Blueprint (Stabilization-First, No Behavior Change)

## Objective
Stabilize MTS by extracting existing behavior into explicit layers while preserving current functionality and operator workflows.

Target structure:

```text
app/
  domain/      (workflow logic)
  services/    (IO, file generation, external actions)
  routes/      (HTTP only)
  models/      (ORM only)
  templates/
```

---

## 1) Module responsibilities

## `app/routes/` (HTTP adapters only)
- Parse request/form/query payloads.
- Call domain/services.
- Handle auth/role guards.
- Return templates/redirect/JSON.
- No direct business calculations, inventory mutations, or lifecycle transitions.

Suggested route modules:
- `routes/production.py`
- `routes/engineering.py`
- `routes/stations.py`
- `routes/maintenance.py`
- `routes/inventory.py`
- `routes/admin.py`
- `routes/entity_admin.py` (generic CRUD, constrained)

## `app/domain/` (manufacturing workflow logic)
- **Single source of truth for lifecycle state transitions.**
- Pallet lifecycle state machine: staged → queued → in_progress → complete/hold/combined.
- Station queue progression, split/combine policies.
- Component completion/scrap semantics.
- Inventory side-effect policy (when/how quantities move).
- Maintenance request state progression rules.
- Dashboard metric definitions (what counts as bottleneck, low stock, etc.).

Suggested domain modules:
- `domain/pallet_lifecycle.py`
- `domain/station_execution.py`
- `domain/engineering_handoff.py`
- `domain/inventory_rules.py`
- `domain/maintenance_flow.py`
- `domain/dashboard_metrics.py`

## `app/services/` (IO + external actions)
- Traveler generation and file writes.
- Uploaded file storage and naming policy.
- MPF/PDF parsing adapters.
- Git/admin runtime operations.
- Repository/path runtime settings read/write.

Suggested service modules:
- `services/traveler_service.py`
- `services/file_storage_service.py`
- `services/cutsheet_parser_service.py`
- `services/metrics_service.py` (query-heavy aggregations)
- `services/admin_runtime_service.py`

## `app/models/` (ORM only)
- SQLAlchemy model definitions only.
- No workflow methods, no state transition logic, no side effects.
- Keep model compatibility during migration by moving `app/models.py` into package form (or re-exporting during transition).

---

## 2) Dependency rules

Hard rules:
1. `routes` may import `domain`, `services`, `models`, auth/session utilities.
2. `domain` may import `models` and service interfaces; it must not import route/template concerns.
3. `services` may import `models` and infrastructure libs; must not import routes.
4. `models` import only SQLAlchemy/base dependencies.
5. `templates` are referenced only by `routes`.

### Allowed imports matrix

| From \ To | routes | domain | services | models | templates |
|---|---:|---:|---:|---:|---:|
| routes | ✅ | ✅ | ✅ | ✅ | ✅ |
| domain | ❌ | ✅ | ✅* | ✅ | ❌ |
| services | ❌ | ❌ | ✅ | ✅ | ❌ |
| models | ❌ | ❌ | ❌ | ✅ | ❌ |
| templates | ❌ | ❌ | ❌ | ❌ | n/a |

`✅*` = via explicit service interfaces/facades (not arbitrary cross-calls).

---

## 3) Core workflows diagram (text-based)

## A) Pallet lifecycle

```text
[Engineering data ready]
      ↓
[Production creates order/pallet]
      ↓
[PalletLifecycleDomain.initialize_pallet()]
  - route assignment
  - component snapshot
  - pallet parts seed
  - initial events
      ↓
[release_to_queue]
  - inventory reservation/update policy
  - queue row creation
      ↓
[station start]
      ↓
[station complete]
  - component logs
  - qty/scrap application
  - optional split policy
  - route progression
      ↓
[complete OR next queue OR storage]
      ↓
[TravelerService.generate(pallet_id, trigger)]
```

## B) Engineering handoff

```text
[Upload PDF/MPF]
    ↓
[CutsheetParserService.parse()]
    ↓
[EngineeringHandoffDomain.apply_parsed_payload()]
  - part master update
  - revision header update
  - BOM upsert
  - MPF master/detail upsert
  - process file links
```

## C) Metrics flow

```text
[Route GET dashboard/stations/inventory]
    ↓
[MetricsService aggregate queries]
    ↓
[Domain dashboards normalize/label]
    ↓
[Route render template]
```

---

## 4) Operator UI vs Engineering UI separation

## Operator UI (execution-critical)
- `/production`, `/stations`, station login/complete, queue actions.
- Must depend on `domain/pallet_lifecycle` and `domain/station_execution` only.
- Keep write paths tightly constrained to lifecycle APIs.

## Engineering UI (definition-centric)
- `/engineering/*` part/revision/BOM/file tools.
- Must depend on `domain/engineering_handoff` + parsing/file services.
- No direct modification of active queue or station execution state.

## Shared but controlled
- Inventory/maintenance can interact with execution only through domain APIs with explicit intents.

---

## 5) AI-friendly ownership model (future-safe)

## Ownership boundaries
- `domain/*` owns **business invariants** (AI agents should add workflow changes here first).
- `services/*` owns **IO/integration** (AI agents should add file/external interactions here).
- `routes/*` owns **HTTP/UI wiring** only (AI agents should not put rules here).
- `models/*` owns **schema mapping** (AI agents should keep logic out).

## Where new changes should go
- **New manufacturing workflow:** `domain/` + tests around state transitions.
- **UI behavior/layout changes:** `routes/` (request/viewmodel) + `templates/`.
- **Schema changes:** `models/` + migration scripts; never runtime ALTER in startup.
- **Parser/integration work:** `services/`.

## Guardrails for autonomous contributors
- One workflow = one domain module owner file.
- Every route method should map to one domain operation and optional service call.
- Ban direct ORM mutation in templates/routes except simple read-only list/detail views.
- Keep generic CRUD away from execution-critical entities unless routed via domain policies.

---

## 6) Migration strategy from current structure

## Principles
- Behavior parity first.
- Extract before optimize.
- Keep route paths and templates stable initially.
- Introduce adapters/wrappers to avoid big-bang rewrites.

## Strategy summary
1. Create new package layout without deleting existing functions.
2. Move pure helper functions first (no behavior changes).
3. Wrap high-risk lifecycle logic behind domain facades.
4. Switch routes to call facades incrementally.
5. Keep database schema and templates unchanged until domain stabilization is complete.

