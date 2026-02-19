# DEAD END REPORT

Scope: Identification of partially implemented features, disconnected routes/templates, schema areas with weak/unclear workflow usage, and boundary violations.

---

## 1) Engineering machine-program pages are explicit stubs
- **File(s):** `app/main.py` (`/engineering/wj-gcode`, `/engineering/abb-modules`, `/engineering/drawings`), `app/templates/engineering_machine_program_stub.html`
- **Why dead end:** Routes intentionally return a stub template with “coming next” messaging and no functional workflow.
- **Risk level:** MEDIUM
- **Recommended action:** **isolate** (keep visible but isolate in roadmap as non-production pathway)

## 2) `/engineering/hk-mpfs/parse` parse-only endpoint not integrated to persistence
- **File(s):** `app/main.py`
- **Why dead end:** Endpoint validates/parses uploads and returns JSON, but it does not persist records; overlaps with `/engineering/parts/upload-pdf` workflow that does persist.
- **Risk level:** MEDIUM
- **Recommended action:** **refactor later** (merge into one ingestion pipeline or clearly mark as diagnostics-only)

## 3) Legacy maintenance status mapper is unused
- **File(s):** `app/main.py` (`normalize_maintenance_status`, `LEGACY_MAINTENANCE_STATUS_MAP`, `MAINTENANCE_ACTIVE_STATUSES`)
- **Why dead end:** Definitions exist but no execution path invokes `normalize_maintenance_status`; this indicates abandoned status migration logic.
- **Risk level:** LOW
- **Recommended action:** **deprecate** (remove after verifying no external callers depend on it)

## 4) Duplicate split workflows with divergent semantics
- **File(s):** `app/main.py` (`/stations/{id}/complete` spawn_leftover, `/pallets/{pallet_id}/split`)
- **Why dead end:** Two independent split implementations produce different quantities/fields and lifecycle behavior, increasing inconsistency risk.
- **Risk level:** HIGH
- **Recommended action:** **refactor later** (converge to one domain split policy)

## 5) Duplicate pallet deletion paths
- **File(s):** `app/main.py` (`/production/pallet/{id}/delete`, generic `/entity/{entity}/{item_id}/delete` for pallets)
- **Why dead end:** Same destructive workflow implemented twice; one removes linked order, the other only marks/cancels in some cases.
- **Risk level:** HIGH
- **Recommended action:** **refactor later** (single deletion domain service)

## 6) Generic CRUD over core production entities bypasses workflow guardrails
- **File(s):** `app/main.py` (`/entity/*` + `MODEL_MAP` + role permissions)
- **Why dead end:** Generic writes can modify core lifecycle tables (`pallets`, `queues`, `pallet_events`, etc.) outside dedicated domain rules.
- **Risk level:** HIGH
- **Recommended action:** **isolate** (limit to non-execution entities first; eventually split admin data model from production execution model)

## 7) Startup-time schema migration pattern indicates unfinished migration strategy
- **File(s):** `app/main.py` (`ensure_*_schema`, startup hook)
- **Why dead end:** Runtime ALTER/CREATE logic suggests unfinished migration framework and causes non-deterministic schema evolution.
- **Risk level:** MEDIUM
- **Recommended action:** **refactor later** (move to explicit migration pipeline)

## 8) Unused/disconnected inventory templates by route mapping
- **File(s):** `app/templates/consumables_inventory.html`, `app/templates/consumable_detail.html` (by static mapping check vs current route template calls)
- **Why dead end:** Current route-template mapping indicates these templates are not referenced in active handlers.
- **Risk level:** LOW
- **Recommended action:** **deprecate** (or reconnect with explicit route ownership)

## 9) Dual part lineage models create partial overlap
- **File(s):** `app/models.py` (`Part`/`PartRevision` and `PartMaster`/`RevisionHeader`/`RevisionBom` families), `app/main.py`
- **Why dead end:** Engineering and production workflows rely on two part representations with translation/bridging logic, signaling incomplete consolidation.
- **Risk level:** HIGH
- **Recommended action:** **keep** (needed now), then **refactor later** under an explicit anti-corruption layer

## 10) Traveler generation as ad-hoc text writer in route module
- **File(s):** `app/main.py` (`create_traveler_file`)
- **Why dead end:** Critical traceability function is implemented as ad-hoc text output and invoked from selected routes only; not lifecycle-governed.
- **Risk level:** MEDIUM
- **Recommended action:** **isolate** (extract to service and define canonical invocation points)

## 11) Orphan-prone data area: delivered lots and scrap steel disconnected from pallet completion core
- **File(s):** `app/models.py` (`DeliveredPartLot`, `ScrapSteel`), `app/main.py` inventory routes
- **Why dead end:** Data structures exist but are not tightly integrated with station completion closure workflow, suggesting manual side-channel tracking.
- **Risk level:** MEDIUM
- **Recommended action:** **keep** (operationally useful), but **refactor later** for lifecycle linkage

## 12) Admin server-maintenance concerns mixed into manufacturing app runtime
- **File(s):** `app/main.py` (`/admin/server-maintenance`)
- **Why dead end:** Git branch operations and runtime path mutation are infrastructure concerns embedded in business app handler.
- **Risk level:** MEDIUM
- **Recommended action:** **isolate** (retain capability; move to ops service boundary later)

