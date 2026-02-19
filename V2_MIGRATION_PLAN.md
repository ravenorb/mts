# V2 Safe Migration Plan (Stability > Elegance)

This plan preserves current behavior and prioritizes low-risk extraction with maximum operational benefit.

---

## Refactor order (step-by-step)

## Phase 0 — Baseline and safeguards (lowest risk, do first)
1. **Create architecture folders and facades only**
   - Add `app/domain`, `app/services`, `app/routes`, `app/models` package scaffolding.
   - Re-export current model classes to avoid import breakage.
   - **Effort:** 0.5–1 day.
2. **Freeze behavior with route-level characterization checklist**
   - Document expected behavior for critical routes (production create/release, station start/complete, maintenance save).
   - **Effort:** 0.5 day.

## Phase 1 — Priority #1: pallet lifecycle extraction
3. **Extract pallet lifecycle helpers from `main.py` into `domain/pallet_lifecycle.py`**
   - Move, unchanged: routing generation, queue enqueue, storage-bin assignment helpers, inventory release/rollback calls orchestration.
   - Keep old function names as wrappers initially.
   - **Effort:** 1.5–2.5 days.
4. **Unify split/combine/deletion orchestration under lifecycle domain API**
   - Introduce canonical domain operations:
     - `create_manual_pallet`
     - `create_order_and_pallet`
     - `release_pallet`
     - `delete_pallet`
     - `split_pallet`
     - `combine_pallets`
   - Route handlers become adapters.
   - **Effort:** 2–3 days.

## Phase 2 — Priority #2: traveler generation isolation
5. **Move traveler writing into `services/traveler_service.py`**
   - Keep exact output content/path behavior.
   - Replace direct route calls with service invocation from domain lifecycle triggers.
   - **Effort:** 0.5–1 day.
6. **Define traveler trigger policy in domain**
   - Centralize “when traveler regenerates” to avoid route drift.
   - **Effort:** 0.5 day.

## Phase 3 — Priority #3: dashboard metrics service
7. **Extract dashboard/station/inventory aggregations into `services/metrics_service.py`**
   - Move SQL aggregation blocks from `/`, `/stations`, `/inventory` routes.
   - Preserve existing values and labels.
   - **Effort:** 1–2 days.
8. **Create `domain/dashboard_metrics.py` view-model normalization layer**
   - Keep formatting logic (percentages/load cards) outside routes.
   - **Effort:** 0.5–1 day.

## Phase 4 — Engineering handoff stabilization
9. **Extract parser + file IO into services**
   - `cutsheet_parser_service`, `file_storage_service`.
   - Route functions stop handling byte-level file logic directly.
   - **Effort:** 1.5–2.5 days.
10. **Extract engineering upsert orchestration to domain**
    - Part master/revision/BOM/header/MPF coordination in one place.
    - **Effort:** 1.5–2 days.

## Phase 5 — Maintenance and inventory boundary cleanup
11. **Move maintenance state transitions to `domain/maintenance_flow.py`**
    - Scheduled generation, completion side-effects, task next_due updates.
    - **Effort:** 1–1.5 days.
12. **Move inventory business rules to `domain/inventory_rules.py`**
    - Standardize release/delete/consumable updates.
    - **Effort:** 1–1.5 days.

## Phase 6 — Route split and admin isolation
13. **Split `main.py` routes into domain-specific route modules**
    - Register routers centrally.
    - **Effort:** 1.5–2.5 days.
14. **Isolate server-maintenance operations**
    - Move git/path operations into `services/admin_runtime_service.py` with explicit enable flags.
    - **Effort:** 0.5–1 day.

## Phase 7 — Migration framework (later, controlled)
15. **Replace startup schema mutation with migration scripts**
    - Keep current startup checks during transition window, then deprecate.
    - **Effort:** 2–3 days.

---

## Highest-impact-first checkpoints

1. **Pallet lifecycle domain API live with unchanged routes.**
2. **Traveler generation invoked only via lifecycle domain events.**
3. **Metrics service powering dashboard/stations/inventory pages.**

These three provide maximum stability payoff with minimal UI disruption.

---

## What NOT to touch early

1. **Do not redesign database schema in early phases.**
2. **Do not rename route URLs or template filenames in early phases.**
3. **Do not alter station operator UI flow before lifecycle extraction is complete.**
4. **Do not remove generic CRUD until production-safe replacements exist.**
5. **Do not “optimize” query behavior while extracting modules; preserve semantics first.**

---

## Effort summary (rough)
- **Foundational extraction (Phases 0–3):** ~7–12 working days.
- **Broader domain/service split (Phases 4–6):** ~6–10 working days.
- **Migration framework hardening (Phase 7):** ~2–3 working days.
- **Total stabilization path:** ~15–25 working days depending on regression/test depth.

