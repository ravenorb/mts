import json
import math
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func, or_, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import hash_password, verify_password
from .database import Base, engine, get_db
from . import models

app = FastAPI(title="Manufacturing Tracking System")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "change-me"))
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = Path(os.getenv("MTS_RUNTIME_SETTINGS_PATH", "/data/config/runtime_settings.json"))


def load_runtime_settings() -> dict:
    try:
        if SETTINGS_PATH.exists():
            return json.loads(SETTINGS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {}


def save_runtime_settings(settings: dict) -> bool:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
        return True
    except OSError:
        return False


RUNTIME_SETTINGS = load_runtime_settings()
DRAWING_DIR = Path(RUNTIME_SETTINGS.get("DRAWING_DATA_PATH") or os.getenv("DRAWING_DATA_PATH", "/data/drawings"))
PDF_DIR = Path(RUNTIME_SETTINGS.get("PDF_DATA_PATH") or os.getenv("PDF_DATA_PATH", "/data/pdfs"))
PART_FILE_DIR = Path(RUNTIME_SETTINGS.get("PART_FILE_DATA_PATH") or os.getenv("PART_FILE_DATA_PATH", "/data/part_revision_files"))
DRAWING_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)
PART_FILE_DIR.mkdir(parents=True, exist_ok=True)


def run_git_command(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=False, cwd=REPO_ROOT)
    except FileNotFoundError:
        return None


def run_post_pull_command() -> tuple[bool, str]:
    command = (os.getenv("MTS_PULL_APPLY_COMMAND") or "").strip()
    if not command:
        return False, "No apply command configured"
    try:
        subprocess.Popen(
            ["bash", "-lc", f"sleep 1; {command}"],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return False, f"Failed to queue apply command: {exc}"
    return True, "Apply command queued"


def list_branches() -> tuple[list[str], str]:
    branch_result = run_git_command(["branch", "--all", "--format=%(refname:short)"])
    branch_lines = branch_result.stdout.splitlines() if branch_result else []
    branch_lookup = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"])
    active_branch = (branch_lookup.stdout.strip() if branch_lookup else "main") or "main"

    branches: list[str] = []
    seen: set[str] = set()
    for line in branch_lines:
        name = line.strip()
        if not name or "->" in name:
            continue
        if name.startswith("remotes/"):
            name = name.replace("remotes/origin/", "", 1)
        if name not in seen:
            branches.append(name)
            seen.add(name)

    if active_branch not in seen:
        branches.insert(0, active_branch)
    if "main" not in seen:
        branches.insert(0, "main")
    return branches, active_branch

MODEL_MAP = {
    "parts": models.Part,
    "part_revisions": models.PartRevision,
    "part_revision_files": models.PartRevisionFile,
    "engineering_questions": models.EngineeringQuestion,
    "part_process_definitions": models.PartProcessDefinition,
    "boms": models.BillOfMaterial,
    "cut_sheets": models.CutSheet,
    "cut_sheet_revisions": models.CutSheetRevision,
    "cut_sheet_revision_outputs": models.CutSheetRevisionOutput,
    "production_orders": models.ProductionOrder,
    "stations": models.Station,
    "queues": models.Queue,
    "pallets": models.Pallet,
    "pallet_revisions": models.PalletRevision,
    "pallet_parts": models.PalletPart,
    "pallet_events": models.PalletEvent,
    "pallet_component_station_logs": models.PalletComponentStationLog,
    "maintenance_requests": models.MaintenanceRequest,
    "station_maintenance_tasks": models.StationMaintenanceTask,
    "maintenance_logs": models.MaintenanceLog,
    "consumables": models.Consumable,
    "purchase_requests": models.PurchaseRequest,
    "purchase_request_lines": models.PurchaseRequestLine,
    "consumable_usage_logs": models.ConsumableUsageLog,
    "storage_locations": models.StorageLocation,
    "storage_bins": models.StorageBin,
    "raw_materials": models.RawMaterial,
    "scrap_steel": models.ScrapSteel,
    "part_inventory": models.PartInventory,
    "delivered_part_lots": models.DeliveredPartLot,
    "employees": models.Employee,
    "skills": models.Skill,
    "employee_skills": models.EmployeeSkill,
    "part_master": models.PartMaster,
    "revision_bom": models.RevisionBom,
    "revision_headers": models.RevisionHeader,
}

ROLE_WRITE = {
    "operator": {"pallets", "pallet_parts", "pallet_events", "queues"},
    "maintenance": {"maintenance_requests", "station_maintenance_tasks", "pallet_events"},
    "purchasing": {"consumables", "purchase_requests", "purchase_request_lines", "consumable_usage_logs"},
    "planner": set(MODEL_MAP.keys()),
    "admin": set(MODEL_MAP.keys()),
}

FIELD_CHOICES = {
    ("employees", "role"): ["operator", "maintenance", "purchasing", "planner", "admin"],
    ("part_revisions", "is_current"): ["true", "false"],
    ("cut_sheet_revisions", "is_current"): ["true", "false"],
    ("stations", "active"): ["true", "false"],
    ("storage_locations", "pallet_storage"): ["true", "false"],
    ("scrap_steel", "delivered"): ["true", "false"],
    ("pallets", "status"): ["staged", "queued", "in_progress", "hold", "complete", "combined"],
    ("pallets", "pallet_type"): ["manual", "split", "mixed"],
    ("queues", "status"): ["queued", "in_progress", "blocked", "done"],
    ("maintenance_requests", "priority"): ["low", "normal", "high", "urgent"],
    ("maintenance_requests", "status"): ["submitted", "reviewed", "scheduled", "waiting on parts", "complete"],
    ("stations", "station_status"): ["ready/idle", "ready/running", "down/repair", "down/wait part", "down/other"],
    ("purchase_requests", "status"): ["open", "approved", "ordered", "received", "closed"],
    ("engineering_questions", "status"): ["open", "answered", "closed"],
}

TOP_NAV = [
    ("Dashboard", "/"),
    ("Production", "/production"),
    ("Engineering", "/engineering"),
    ("Stations", "/stations"),
    ("Inventory", "/inventory"),
    ("Purchasing", "/entity/purchase_requests"),
    ("Maintenance", "/maintenance"),
    ("Admin", "/admin"),
]

ENTITY_GROUPS = {
    "Production": ["pallets", "pallet_parts", "pallet_events", "queues", "production_orders"],
    "Engineering": ["parts", "part_master", "revision_bom", "revision_headers", "part_revisions", "part_revision_files", "engineering_questions", "part_process_definitions", "cut_sheets", "cut_sheet_revisions", "cut_sheet_revision_outputs", "boms"],
    "Maintenance": ["maintenance_requests", "station_maintenance_tasks"],
    "Inventory": ["storage_locations", "raw_materials", "consumables", "parts", "delivered_part_lots", "scrap_steel"],
    "People": ["employees", "skills", "employee_skills"],
}


def engineering_nav_context() -> dict:
    return {
        "engineering_sections": [
            {"label": "Overview", "href": "/engineering"},
            {"label": "Parts", "href": "/engineering/parts"},
            {"label": "HK MPFs", "href": "/engineering/hk-mpfs"},
            {"label": "HK Cut Planner", "href": "/engineering/hk-mpf/cutplanner"},
            {"label": "WJ Gcode", "href": "/engineering/wj-gcode"},
            {"label": "ABB Modules", "href": "/engineering/abb-modules"},
            {"label": "PDFs", "href": "/engineering/pdfs"},
            {"label": "Drawings", "href": "/engineering/drawings"},
        ]
    }

MAINTENANCE_ACTIVE_STATUSES = ["submitted", "reviewed", "scheduled", "waiting on parts"]
LEGACY_MAINTENANCE_STATUS_MAP = {
    "open": "submitted",
    "in_progress": "reviewed",
    "closed": "complete",
}


def ensure_upcoming_scheduled_requests(db: Session):
    now = datetime.utcnow()
    due_by = now + timedelta(days=14)
    tasks = db.query(models.StationMaintenanceTask).filter_by(active=True).all()
    for task in tasks:
        if task.next_due_at is None:
            task.next_due_at = now + timedelta(hours=task.frequency_hours)
        if task.next_due_at > due_by:
            continue
        existing = db.query(models.MaintenanceRequest).filter(
            models.MaintenanceRequest.maintenance_task_id == task.id,
            models.MaintenanceRequest.request_type == "scheduled",
            models.MaintenanceRequest.status != "complete",
        ).first()
        if existing:
            continue
        db.add(models.MaintenanceRequest(
            station_id=task.station_id,
            maintenance_task_id=task.id,
            requested_by="system",
            priority="normal",
            status="scheduled",
            issue_description=task.task_description,
            request_type="scheduled",
            scheduled_for=task.next_due_at,
        ))
    db.commit()


def normalize_maintenance_status(item: models.MaintenanceRequest):
    mapped = LEGACY_MAINTENANCE_STATUS_MAP.get(item.status)
    if mapped:
        item.status = mapped


def fk_choices(col, db: Session):
    fk = next(iter(col.foreign_keys), None)
    if not fk:
        return None
    table_name = fk.column.table.name
    label_columns = ["pallet_code", "station_name", "part_number", "revision_code", "cut_sheet_number", "username", "employee_code", "description", "name"]
    for entity_name, model in MODEL_MAP.items():
        if model.__table__.name != table_name:
            continue
        rows = db.query(model).limit(300).all()
        options = []
        for row in rows:
            label = next((str(getattr(row, attr)) for attr in label_columns if hasattr(row, attr) and getattr(row, attr) not in (None, "")), f"{table_name}:{row.id}")
            options.append({"value": str(row.id), "label": f"{row.id} — {label}"})
        return options
    return None


def build_field_meta(entity: str, col, db: Session):
    choices = FIELD_CHOICES.get((entity, col.name), None)
    if isinstance(col.type, Boolean):
        choices = ["true", "false"]

    expected = "Free text"
    if isinstance(col.type, Integer):
        expected = "Whole number (example: 5)"
    elif isinstance(col.type, Float):
        expected = "Number (example: 12.5)"
    elif isinstance(col.type, Boolean):
        expected = "Choose true or false"
    elif isinstance(col.type, DateTime):
        expected = "Date/time in ISO format (example: 2026-01-31T14:30:00)"
    elif isinstance(col.type, String):
        expected = f"Text up to {col.type.length} characters" if col.type.length else "Text"
    elif isinstance(col.type, Text):
        expected = "Long text"

    required = (not col.nullable) and col.default is None and col.server_default is None

    return {
        "name": col.name,
        "required": required,
        "expected": expected,
        "choices": choices,
        "fk_choices": fk_choices(col, db),
    }


def parse_field_value(entity: str, col, raw_value):
    if raw_value is None:
        return None

    val = raw_value.strip() if isinstance(raw_value, str) else raw_value
    if val == "":
        return None

    choices = FIELD_CHOICES.get((entity, col.name), None)
    if isinstance(col.type, Boolean):
        lowered = str(val).strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        raise ValueError("must be true or false")

    if choices and str(val) not in choices:
        raise ValueError(f"must be one of: {', '.join(choices)}")

    if isinstance(col.type, Integer):
        try:
            return int(val)
        except ValueError as exc:
            raise ValueError("must be a whole number") from exc

    if isinstance(col.type, Float):
        try:
            return float(val)
        except ValueError as exc:
            raise ValueError("must be a number") from exc

    if isinstance(col.type, DateTime):
        try:
            return datetime.fromisoformat(str(val))
        except ValueError as exc:
            raise ValueError("must be an ISO date/time like 2026-01-31T14:30:00") from exc

    if isinstance(col.type, String) and col.type.length and len(str(val)) > col.type.length:
        raise ValueError(f"must be at most {col.type.length} characters")

    return val


def create_default_admin(db: Session):
    if not db.query(models.Employee).filter_by(username="admin").first():
        db.add(models.Employee(
            employee_code="ADMIN",
            full_name="Administrator",
            phone_number="",
            email_address="admin@local",
            username="admin",
            password_hash=hash_password("admin123"),
            role="admin",
            active=True,
        ))
        db.commit()


def ensure_employee_auth_schema(db: Session):
    employee_columns = {row[1] for row in db.execute(text("PRAGMA table_info(employees)"))}
    if "username" not in employee_columns:
        db.execute(text("ALTER TABLE employees ADD COLUMN username VARCHAR(64)"))
    if "password_hash" not in employee_columns:
        db.execute(text("ALTER TABLE employees ADD COLUMN password_hash VARCHAR(255) DEFAULT ''"))
    db.commit()


def migrate_users_to_employees(db: Session):
    users = db.query(models.User).all()
    if not users:
        return

    touched = False
    for account in users:
        employee = None
        if account.username:
            employee = db.query(models.Employee).filter_by(username=account.username).first()
        if not employee:
            employee = db.query(models.Employee).filter_by(user_id=account.id).first()

        if employee:
            if not employee.username:
                employee.username = account.username
                touched = True
            if not employee.password_hash:
                employee.password_hash = account.password_hash
                touched = True
            if account.role and employee.role != account.role:
                employee.role = account.role
                touched = True
            if employee.active != account.active:
                employee.active = account.active
                touched = True
            continue

        employee_code = f"EMP{account.id:04d}"
        if db.query(models.Employee).filter_by(employee_code=employee_code).first():
            employee_code = f"EMP{int(datetime.utcnow().timestamp())}{account.id}"
        email = f"{account.username}@local"
        if db.query(models.Employee).filter_by(email_address=email).first():
            email = f"{account.username}-{account.id}@local"

        db.add(models.Employee(
            employee_code=employee_code,
            full_name=account.username,
            phone_number="",
            email_address=email,
            username=account.username,
            password_hash=account.password_hash,
            user_id=account.id,
            role=account.role,
            active=account.active,
        ))
        touched = True

    if touched:
        db.commit()


def ensure_station_schema(db: Session):
    station_columns = {row[1] for row in db.execute(text("PRAGMA table_info(stations)"))}
    if "station_code" not in station_columns:
        db.execute(text("ALTER TABLE stations ADD COLUMN station_code VARCHAR(2) DEFAULT ''"))
    if "station_status" not in station_columns:
        db.execute(text("ALTER TABLE stations ADD COLUMN station_status VARCHAR(40) DEFAULT 'ready/idle'"))
    db.commit()


def ensure_pallet_schema(db: Session):
    pallet_columns = {row[1] for row in db.execute(text("PRAGMA table_info(pallets)"))}
    if "mpf_master_id" not in pallet_columns:
        db.execute(text("ALTER TABLE pallets ADD COLUMN mpf_master_id INTEGER"))
    if "frame_part_number" not in pallet_columns:
        db.execute(text("ALTER TABLE pallets ADD COLUMN frame_part_number VARCHAR(80) DEFAULT ''"))
    if "expected_quantity" not in pallet_columns:
        db.execute(text("ALTER TABLE pallets ADD COLUMN expected_quantity FLOAT DEFAULT 0"))
    if "sheet_count" not in pallet_columns:
        db.execute(text("ALTER TABLE pallets ADD COLUMN sheet_count FLOAT DEFAULT 0"))
    if "component_list_json" not in pallet_columns:
        db.execute(text("ALTER TABLE pallets ADD COLUMN component_list_json TEXT DEFAULT '[]'"))
    if "storage_bin_id" not in pallet_columns:
        db.execute(text("ALTER TABLE pallets ADD COLUMN storage_bin_id INTEGER"))
    db.commit()


def ensure_pallet_parts_schema(db: Session):
    pallet_part_columns = {row[1] for row in db.execute(text("PRAGMA table_info(pallet_parts)"))}
    if "external_quantity_needed" not in pallet_part_columns:
        db.execute(text("ALTER TABLE pallet_parts ADD COLUMN external_quantity_needed FLOAT DEFAULT 0"))
    db.commit()


def ensure_pallet_station_route_schema(db: Session):
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS pallet_station_routes (
            id INTEGER PRIMARY KEY,
            pallet_id INTEGER NOT NULL,
            sequence_no INTEGER DEFAULT 1,
            station_id INTEGER,
            qty_completed FLOAT DEFAULT 0,
            qty_scrap FLOAT DEFAULT 0,
            status VARCHAR(40) DEFAULT 'staged',
            location_id VARCHAR(20) DEFAULT '00',
            FOREIGN KEY(pallet_id) REFERENCES pallets(id),
            FOREIGN KEY(station_id) REFERENCES stations(id)
        )
    """))
    db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_pallet_station_sequence ON pallet_station_routes(pallet_id, sequence_no)"))
    db.commit()




def ensure_pallet_component_station_log_schema(db: Session):
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS pallet_component_station_logs (
            id INTEGER PRIMARY KEY,
            pallet_id INTEGER NOT NULL,
            station_id INTEGER NOT NULL,
            component_id VARCHAR(80) DEFAULT '',
            qty_expected FLOAT DEFAULT 0,
            qty_completed FLOAT DEFAULT 0,
            qty_scrap FLOAT DEFAULT 0,
            recorded_by VARCHAR(80) DEFAULT 'system',
            recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(pallet_id) REFERENCES pallets(id),
            FOREIGN KEY(station_id) REFERENCES stations(id)
        )
    """))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_component_station_log_pallet_station ON pallet_component_station_logs(pallet_id, station_id)"))
    db.commit()

def ensure_storage_bin_schema(db: Session):
    storage_bin_columns = {row[1] for row in db.execute(text("PRAGMA table_info(storage_bins)"))}
    if "location_id" not in storage_bin_columns:
        db.execute(text("ALTER TABLE storage_bins ADD COLUMN location_id VARCHAR(80) DEFAULT ''"))
    if "component_id" not in storage_bin_columns:
        db.execute(text("ALTER TABLE storage_bins ADD COLUMN component_id VARCHAR(80) DEFAULT ''"))

    if "pallet_id" in storage_bin_columns:
        db.execute(text("UPDATE storage_bins SET location_id = COALESCE(NULLIF(location_id, ''), COALESCE(pallet_id, ''))"))
    if "part_number" in storage_bin_columns:
        db.execute(text("UPDATE storage_bins SET component_id = COALESCE(NULLIF(component_id, ''), COALESCE(part_number, ''))"))
    db.commit()


def ensure_default_stations(db: Session) -> list[models.Station]:
    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    if stations:
        updated = False
        for idx, station in enumerate(stations, start=1):
            if not station.station_code:
                station.station_code = f"{idx:02d}"[-2:]
                updated = True
            if not station.station_status:
                station.station_status = "ready/idle"
                updated = True
        if updated:
            db.commit()
        return stations
    db.add_all([
        models.Station(station_code="01", station_name="station1", skill_required="", station_status="ready/idle"),
        models.Station(station_code="02", station_name="station2", skill_required="", station_status="ready/idle"),
    ])
    db.commit()
    return db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()




def get_part_component_requirements(db: Session, part_id: str) -> list[dict]:
    if not part_id:
        return []
    part_master = db.query(models.PartMaster).filter_by(part_id=part_id).first()
    if not part_master:
        return []
    bom_lines = db.query(models.RevisionBom).filter_by(part_id=part_id, rev_id=part_master.cur_rev).order_by(models.RevisionBom.id.asc()).all()
    components: list[dict] = []
    for line in bom_lines:
        comp_id = (line.comp_id or '').strip()
        if not comp_id:
            continue
        components.append({
            'component_id': comp_id,
            'component_qty': float(line.comp_qty or 0),
        })
    return components

def get_part_station_routes(db: Session, part_number: str) -> list[models.Station]:
    if not part_number:
        return []
    stations = db.query(models.Station).join(
        models.PartStationRoute,
        models.PartStationRoute.station_id == models.Station.id,
    ).filter(
        models.PartStationRoute.part_id == part_number,
        models.Station.active.is_(True),
    ).order_by(models.PartStationRoute.route_order.asc(), models.PartStationRoute.id.asc()).all()
    return stations


def get_pallet_part_rows(db: Session, pallet: models.Pallet) -> list[dict]:
    part_rows = []
    pallet_parts = db.query(models.PalletPart).filter_by(pallet_id=pallet.id).order_by(models.PalletPart.id.asc()).all()
    for pallet_part in pallet_parts:
        revision = db.query(models.PartRevision).filter_by(id=pallet_part.part_revision_id).first()
        component_id = ""
        if revision:
            part_row = db.query(models.Part).filter_by(id=revision.part_id).first()
            component_id = part_row.part_number if part_row else ""

        part_rows.append({
            "expected_qty": pallet_part.planned_quantity,
            "qty_needed": pallet_part.external_quantity_needed,
            "current_qty": pallet_part.actual_quantity,
            "component_id": component_id,
            "scrap_qty": pallet_part.scrap_quantity,
        })

    component_rows = [
        row for row in part_rows
        if row["component_id"] and row["component_id"] != (pallet.frame_part_number or "")
    ]
    component_rows_by_id = {row["component_id"]: row for row in component_rows}
    try:
        component_snapshot = json.loads(pallet.component_list_json or "[]")
    except json.JSONDecodeError:
        component_snapshot = []

    for component in component_snapshot:
        component_id = (component.get("component_id") or "").strip()
        if not component_id:
            continue
        expected_qty = component.get("expected_quantity", 0)
        existing_row = component_rows_by_id.get(component_id)
        if existing_row:
            if not existing_row["expected_qty"]:
                existing_row["expected_qty"] = expected_qty
            if not existing_row["qty_needed"]:
                existing_row["qty_needed"] = component.get("qty_needed", 0)
            continue
        new_row = {
            "expected_qty": expected_qty,
            "qty_needed": component.get("qty_needed", component.get("external_quantity_needed", 0)),
            "current_qty": 0,
            "component_id": component_id,
            "scrap_qty": 0,
        }
        component_rows.append(new_row)
        component_rows_by_id[component_id] = new_row

    if component_rows:
        component_rows.sort(key=lambda row: row["component_id"])
    else:
        component_rows = part_rows
    return component_rows


def ensure_pallet_station_routing(db: Session, pallet: models.Pallet, fallback_station_id: int | None = None):
    existing_rows = db.query(models.PalletStationRoute).filter_by(pallet_id=pallet.id).order_by(models.PalletStationRoute.sequence_no.asc()).all()
    if existing_rows:
        for row in existing_rows:
            if row.qty_completed is None:
                row.qty_completed = 0
            if row.qty_scrap is None:
                row.qty_scrap = 0
            if not (row.status or "").strip():
                row.status = "staged"
        return

    route_stations = get_part_station_routes(db, pallet.frame_part_number or "")
    route_station_ids: list[int | None] = [station.id for station in route_stations]
    if not route_station_ids and fallback_station_id:
        route_station_ids = [fallback_station_id]

    for sequence_no, station_id in enumerate(route_station_ids, start=1):
        db.add(models.PalletStationRoute(
            pallet_id=pallet.id,
            sequence_no=sequence_no,
            station_id=station_id,
            qty_completed=0,
            qty_scrap=0,
            status="staged",
            location_id="00",
        ))


def station_label(station: models.Station | None) -> str:
    if not station:
        return "-"
    return f"S{station.id} - {station.station_name}"


def get_available_pallet_bins(db: Session, include_bin_id: int | None = None, hk_only: bool = False, exclude_hk: bool = False) -> list[models.StorageBin]:
    query = (
        db.query(models.StorageBin)
        .join(models.StorageLocation, models.StorageBin.storage_location_id == models.StorageLocation.id)
        .filter(models.StorageLocation.pallet_storage.is_(True))
    )
    if hk_only:
        query = query.filter(func.lower(models.StorageLocation.location_description).like("%hk queue%"))
    if exclude_hk:
        query = query.filter(~func.lower(models.StorageLocation.location_description).like("%hk queue%"))

    bins = query.order_by(models.StorageBin.storage_location_id.asc(), models.StorageBin.shelf_id.asc(), models.StorageBin.bin_id.asc()).all()
    return [
        b for b in bins
        if not (b.component_id or "").strip() or (include_bin_id and b.id == include_bin_id)
    ]


def clear_pallet_storage_bin(db: Session, pallet: models.Pallet):
    db.query(models.StorageBin).filter(models.StorageBin.component_id == pallet.pallet_code).update({models.StorageBin.component_id: ""}, synchronize_session=False)


def assign_pallet_to_storage_bin(db: Session, pallet: models.Pallet, storage_bin: models.StorageBin):
    clear_pallet_storage_bin(db, pallet)
    storage_bin.component_id = pallet.pallet_code
    pallet.storage_bin_id = storage_bin.id
    pallet.current_station_id = None


def pallet_location_label(db: Session, pallet: models.Pallet) -> str:
    if pallet.storage_bin_id:
        storage_bin = db.query(models.StorageBin).filter_by(id=pallet.storage_bin_id).first()
        if storage_bin:
            location = db.query(models.StorageLocation).filter_by(id=storage_bin.storage_location_id).first()
            location_name = location.location_description if location else f"Location {storage_bin.storage_location_id}"
            return f"{location_name} (L{storage_bin.storage_location_id}-S{storage_bin.shelf_id}-B{storage_bin.bin_id})"
    if pallet.current_station_id:
        station = db.query(models.Station).filter_by(id=pallet.current_station_id).first()
        return station_label(station)
    return "Unassigned"


def update_inventory_for_released_pallet(db: Session, pallet: models.Pallet):
    if pallet.mpf_master_id and pallet.sheet_count > 0:
        mpf = db.query(models.MpfMaster).filter_by(id=pallet.mpf_master_id).first()
        if mpf:
            parsed_sheet = parse_sheet_size(mpf.sheet_size)
            if parsed_sheet:
                width, length = parsed_sheet
                material_row = db.query(models.RawMaterial).filter(
                    models.RawMaterial.gauge == mpf.material,
                    ((models.RawMaterial.width == width) & (models.RawMaterial.length == length)) |
                    ((models.RawMaterial.width == length) & (models.RawMaterial.length == width)),
                ).first()
                if not material_row or material_row.qty_on_hand < pallet.sheet_count:
                    raise HTTPException(422, "Not enough raw material sheets on hand to release this pallet")
                material_row.qty_on_hand -= pallet.sheet_count

    pallet_parts = db.query(models.PalletPart).filter_by(pallet_id=pallet.id).all()
    for pallet_part in pallet_parts:
        revision = db.query(models.PartRevision).filter_by(id=pallet_part.part_revision_id).first()
        if not revision:
            continue
        inventory = db.query(models.PartInventory).filter_by(part_id=revision.part_id).first()
        if not inventory:
            inventory = models.PartInventory(part_id=revision.part_id)
            db.add(inventory)
            db.flush()
        inventory.qty_on_hand_total += pallet_part.planned_quantity or 0
        inventory.qty_queued_to_cut += pallet_part.planned_quantity or 0


def rollback_inventory_for_deleted_pallet(db: Session, pallet: models.Pallet):
    was_released = db.query(models.PalletEvent).filter_by(pallet_id=pallet.id, event_type="released_to_queue").first() is not None
    if not was_released:
        return

    pallet_parts = db.query(models.PalletPart).filter_by(pallet_id=pallet.id).all()
    for pallet_part in pallet_parts:
        revision = db.query(models.PartRevision).filter_by(id=pallet_part.part_revision_id).first()
        if not revision:
            continue
        inventory = db.query(models.PartInventory).filter_by(part_id=revision.part_id).first()
        if not inventory:
            continue

        qty = float(pallet_part.planned_quantity or 0)
        inventory.qty_on_hand_total = max(0, float(inventory.qty_on_hand_total or 0) - qty)
        inventory.qty_queued_to_cut = max(0, float(inventory.qty_queued_to_cut or 0) - qty)


def ensure_order_backlog_has_pallets(db: Session, stations: list[models.Station]):
    if not stations:
        return

    existing_order_ids = {
        row[0]
        for row in db.query(models.Pallet.production_order_id)
        .filter(models.Pallet.production_order_id.isnot(None))
        .all()
    }
    if not existing_order_ids:
        existing_order_ids = set()

    missing_orders = (
        db.query(models.ProductionOrder)
        .filter(models.ProductionOrder.status.notin_(["cancelled", "complete", "closed"]))
        .filter(models.ProductionOrder.id.notin_(existing_order_ids) if existing_order_ids else text("1=1"))
        .order_by(models.ProductionOrder.created_at.asc(), models.ProductionOrder.id.asc())
        .all()
    )
    if not missing_orders:
        return

    first_station = stations[0]
    for order in missing_orders:
        part_revision = db.query(models.PartRevision).filter_by(id=order.part_revision_id).first()
        part_number = ""
        if part_revision:
            part = db.query(models.Part).filter_by(id=part_revision.part_id).first()
            part_number = part.part_number if part else ""

        pallet = models.Pallet(
            pallet_code=f"P-AUTO-{order.id}",
            pallet_type="manual",
            production_order_id=order.id,
            frame_part_number=part_number,
            expected_quantity=order.quantity_ordered or 0,
            sheet_count=0,
            status="staged",
            current_station_id=first_station.id,
            created_by="system",
        )
        db.add(pallet)
        db.flush()

        if part_revision:
            db.add(models.PalletPart(
                pallet_id=pallet.id,
                part_revision_id=part_revision.id,
                planned_quantity=order.quantity_ordered or 0,
                actual_quantity=0,
                scrap_quantity=0,
            ))

        db.add(models.PalletEvent(
            pallet_id=pallet.id,
            station_id=first_station.id,
            event_type="created",
            quantity=order.quantity_ordered or 0,
            recorded_by="system",
            notes=f"Auto-created from production order {order.id}",
        ))

        max_position = db.query(func.max(models.Queue.queue_position)).filter_by(station_id=first_station.id).scalar() or 0
        db.add(models.Queue(
            station_id=first_station.id,
            pallet_id=pallet.id,
            queue_position=int(max_position) + 1,
            status="queued",
        ))

    db.commit()


def ensure_current_part_revision(db: Session, part_number: str, username: str) -> models.PartRevision:
    part = db.query(models.Part).filter_by(part_number=part_number).first()
    if not part:
        part_master = db.query(models.PartMaster).filter_by(part_id=part_number).first()
        part = models.Part(part_number=part_number, description=part_master.description if part_master else "", active=True)
        db.add(part)
        db.flush()

    revision = db.query(models.PartRevision).filter_by(part_id=part.id, is_current=True).first()
    if revision:
        return revision

    revision = db.query(models.PartRevision).filter_by(part_id=part.id).order_by(models.PartRevision.id.desc()).first()
    if revision:
        return revision

    revision = models.PartRevision(
        part_id=part.id,
        revision_code="R0",
        is_current=True,
        released_by=username,
        change_notes="Auto-created revision",
    )
    db.add(revision)
    db.flush()
    return revision


def station_nav_context(db: Session) -> dict:
    stations = ensure_default_stations(db)
    return {"stations_nav": [{"id": s.id, "name": s.station_name} for s in stations]}


def maintenance_station_nav_context(db: Session) -> dict:
    stations = db.query(models.Station).order_by(models.Station.station_name.asc()).all()
    return {
        "maintenance_stations": [
            {"id": s.id, "name": s.station_name, "code": s.station_code or f"{s.id:02d}"}
            for s in stations
        ]
    }


def get_current_user(request: Request, db: Session):
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.query(models.Employee).filter_by(id=uid, active=True).first()


def require_login(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401)
    return user


def can_write(user, entity):
    return entity in ROLE_WRITE.get(user.role, set())


def require_admin(user=Depends(require_login)):
    if user.role != "admin":
        raise HTTPException(403)
    return user


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    ensure_station_schema(db)
    ensure_pallet_schema(db)
    ensure_pallet_parts_schema(db)
    ensure_pallet_station_route_schema(db)
    ensure_pallet_component_station_log_schema(db)
    ensure_storage_bin_schema(db)
    ensure_employee_auth_schema(db)
    migrate_users_to_employees(db)
    create_default_admin(db)
    ensure_default_stations(db)


@app.get("/", response_class=HTMLResponse)
def root(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    active = db.query(models.Pallet).filter(models.Pallet.status != "complete").count()
    hold = db.query(models.Pallet).filter(models.Pallet.status == "hold").count()
    bottlenecks = db.query(models.Queue.station_id, func.count(models.Queue.id)).group_by(models.Queue.station_id).all()
    maintenance_open = db.query(models.MaintenanceRequest).filter(models.MaintenanceRequest.status != "complete").count()
    low_stock = db.query(models.Consumable).filter(models.Consumable.qty_on_hand <= models.Consumable.reorder_point).count()
    staged = db.query(models.Pallet).filter(models.Pallet.status == "staged").count()
    in_progress = db.query(models.Pallet).filter(models.Pallet.status == "in_progress").count()
    station_rows = db.query(models.Station.id, models.Station.station_name, func.count(models.Queue.id)).outerjoin(models.Queue, models.Queue.station_id == models.Station.id).group_by(models.Station.id, models.Station.station_name).all()
    max_load = max([r[2] for r in station_rows], default=1)
    station_load = [{"id": r[0], "name": r[1], "load": r[2], "percent": int((r[2] / max_load) * 100) if max_load else 0} for r in station_rows]
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "active": active, "hold": hold, "staged": staged, "in_progress": in_progress, "bottlenecks": bottlenecks, "station_load": station_load, "maintenance_open": maintenance_open, "low_stock": low_stock})


def parse_sheet_size(sheet_size: str) -> tuple[float, float] | None:
    numbers = re.findall(r"\d+(?:\.\d+)?", sheet_size or "")
    if len(numbers) < 2:
        return None
    return float(numbers[0]), float(numbers[1])


def get_next_route_row(db: Session, pallet_id: int, station_id: int) -> models.PalletStationRoute | None:
    route_rows = db.query(models.PalletStationRoute).filter_by(pallet_id=pallet_id).order_by(models.PalletStationRoute.sequence_no.asc()).all()
    for idx, row in enumerate(route_rows):
        if row.station_id == station_id:
            return route_rows[idx + 1] if idx + 1 < len(route_rows) else None
    return None


def queue_pallet_for_station(db: Session, pallet: models.Pallet, station_id: int):
    existing = db.query(models.Queue).filter_by(station_id=station_id, pallet_id=pallet.id).first()
    if existing:
        existing.status = "queued"
        return
    max_position = db.query(func.max(models.Queue.queue_position)).filter_by(station_id=station_id).scalar() or 0
    db.add(models.Queue(station_id=station_id, pallet_id=pallet.id, queue_position=int(max_position) + 1, status="queued"))


def build_station_component_rollup(db: Session, pallet_id: int) -> dict[str, list[dict]]:
    logs = db.query(models.PalletComponentStationLog).filter_by(pallet_id=pallet_id).order_by(models.PalletComponentStationLog.id.asc()).all()
    rollup: dict[str, list[dict]] = {}
    for log in logs:
        rollup.setdefault(log.component_id, []).append({
            "station_id": log.station_id,
            "qty_completed": log.qty_completed,
            "qty_scrap": log.qty_scrap,
        })
    return rollup


def parse_pallet_component_list(component_list_json: str | None) -> list[dict]:
    if not component_list_json:
        return []
    try:
        payload = json.loads(component_list_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    parsed: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        parsed.append({
            "component_id": (item.get("component_id") or "").strip(),
            "expected_quantity": item.get("expected_quantity", 0),
            "qty_needed": item.get("qty_needed", item.get("external_quantity_needed", 0)),
        })
    return parsed


def build_component_quantities(db: Session, frame_part_id: str, expected_quantity: float, sheet_count: float, mpf_master_id: int) -> list[dict]:
    component_map: dict[str, dict] = {}

    details = db.query(models.MpfDetail).filter_by(mpf_master_id=mpf_master_id).order_by(models.MpfDetail.id.asc()).all()
    for detail in details:
        component_id = (detail.component_id or "").strip()
        if not component_id:
            continue
        component_map[component_id] = {
            "component_id": component_id,
            "expected_quantity": float(detail.sheet_qty or 0) * float(expected_quantity or 0),
            "qty_needed": 0.0,
        }

    bom_components = get_part_component_requirements(db, frame_part_id)
    for component in bom_components:
        component_id = component["component_id"]
        existing = component_map.setdefault(component_id, {
            "component_id": component_id,
            "expected_quantity": 0.0,
            "qty_needed": 0.0,
        })
        existing["qty_needed"] = float(component.get("component_qty") or 0) * float(expected_quantity or 0)

    return [component_map[key] for key in sorted(component_map.keys())]


def build_station_queue_cards(db: Session, stations: list[models.Station]) -> list[dict]:
    cards: list[dict] = []
    for station in stations:
        queue_rows = db.query(models.Queue).filter_by(station_id=station.id).order_by(models.Queue.queue_position.asc()).all()
        in_progress = next((q for q in queue_rows if q.status == "in_progress"), None)
        current_pallet = db.query(models.Pallet).filter_by(id=in_progress.pallet_id).first() if in_progress else None
        current_operator = ""
        if current_pallet:
            last_started = db.query(models.PalletEvent).filter_by(
                pallet_id=current_pallet.id,
                station_id=station.id,
                event_type="started",
            ).order_by(models.PalletEvent.recorded_at.desc()).first()
            current_operator = last_started.recorded_by if last_started else ""
        waiting = [
            {
                "queue_id": row.id,
                "pallet": db.query(models.Pallet).filter_by(id=row.pallet_id).first(),
                "position": row.queue_position,
            }
            for row in queue_rows if row.status == "queued"
        ]
        cards.append({
            "station": station,
            "current_pallet": current_pallet,
            "current_operator": current_operator,
            "waiting": waiting,
        })
    return cards


@app.get("/production", response_class=HTMLResponse)
def production(request: Request, q: str = "", tab: str = "active", db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = None
    if q:
        pallet_query = db.query(models.Pallet).filter(models.Pallet.pallet_code == q)
        if q.isdigit():
            pallet_query = db.query(models.Pallet).filter((models.Pallet.pallet_code == q) | (models.Pallet.id == int(q)))
        pallet = pallet_query.first()

    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    ensure_order_backlog_has_pallets(db, stations)
    part_revisions = db.query(models.PartRevision).order_by(models.PartRevision.id.desc()).limit(200).all()
    active_pallets = db.query(models.Pallet).order_by(models.Pallet.created_at.desc()).all()
    production_orders = db.query(models.ProductionOrder).order_by(models.ProductionOrder.created_at.desc()).all()

    part_revisions_by_id = {
        row.id: row
        for row in db.query(models.PartRevision).filter(
            models.PartRevision.id.in_([order.part_revision_id for order in production_orders])
        ).all()
    } if production_orders else {}
    part_ids = [revision.part_id for revision in part_revisions_by_id.values()]
    parts_by_id = {
        row.id: row
        for row in db.query(models.Part).filter(models.Part.id.in_(part_ids)).all()
    } if part_ids else {}

    order_rows = []
    for order in production_orders:
        revision = part_revisions_by_id.get(order.part_revision_id)
        part = parts_by_id.get(revision.part_id) if revision else None
        order_rows.append({
            "order": order,
            "part_number": part.part_number if part else "-",
            "revision_code": revision.revision_code if revision else "-",
        })

    selected_station = None
    station_queue = []
    if tab.startswith("station-"):
        station_id = tab.split("station-", 1)[1]
        if station_id.isdigit():
            selected_station = next((station for station in stations if station.id == int(station_id)), None)
    if selected_station:
        queue_rows = db.query(models.Queue).filter_by(station_id=selected_station.id).order_by(models.Queue.queue_position.asc()).all()
        pallets_by_id = {
            pallet.id: pallet
            for pallet in db.query(models.Pallet).filter(models.Pallet.id.in_([row.pallet_id for row in queue_rows])).all()
        }
        for queue_row in queue_rows:
            pallet_for_row = pallets_by_id.get(queue_row.pallet_id)
            if not pallet_for_row:
                continue
            station_queue.append({
                "queue": queue_row,
                "pallet": pallet_for_row,
                "pallet_components": parse_pallet_component_list(pallet_for_row.component_list_json),
            })

    frame_parts_from_mpf = {
        row[0]
        for row in db.query(models.MpfMaster.part_id)
        .filter(models.MpfMaster.part_id.isnot(None), models.MpfMaster.part_id != "")
        .distinct()
        .all()
    }
    frame_parts_from_parts = {
        row[0]
        for row in db.query(models.Part.part_number)
        .filter(models.Part.part_number.isnot(None), models.Part.part_number != "", models.Part.active.is_(True))
        .distinct()
        .all()
    }
    frame_parts_from_part_master = {
        row[0]
        for row in db.query(models.PartMaster.part_id)
        .filter(models.PartMaster.part_id.isnot(None), models.PartMaster.part_id != "")
        .distinct()
        .all()
    }
    frame_parts = sorted(frame_parts_from_mpf | frame_parts_from_parts | frame_parts_from_part_master)

    return templates.TemplateResponse("production.html", {
        "request": request,
        "user": user,
        "top_nav": TOP_NAV,
        "entity_groups": ENTITY_GROUPS,
        "query": q,
        "found": pallet,
        "active_pallets": active_pallets,
        "stations": stations,
        "part_revisions": part_revisions,
        "frame_parts": frame_parts,
        "errors": {},
        "tab": tab,
        "selected_station": selected_station,
        "station_queue": station_queue,
        "order_rows": order_rows,
    })


@app.get("/production/mpf-options/{frame_part_id}", response_class=JSONResponse)
def production_mpf_options(frame_part_id: str, db: Session = Depends(get_db), user=Depends(require_login)):
    masters = db.query(models.MpfMaster).filter_by(part_id=frame_part_id).order_by(models.MpfMaster.mpf_filename.asc()).all()
    return {
        "items": [
            {
                "id": mpf.id,
                "filename": mpf.mpf_filename,
                "qty_produced": mpf.qty_produced,
                "material": mpf.material,
                "sheet_size": mpf.sheet_size,
            }
            for mpf in masters
        ]
    }


@app.get("/production/pallet/{pallet_id}", response_class=HTMLResponse)
def pallet_detail(pallet_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = db.query(models.Pallet).filter_by(id=pallet_id).first()
    if not pallet:
        raise HTTPException(404)
    part_rows = get_pallet_part_rows(db, pallet)
    ensure_pallet_station_routing(db, pallet, fallback_station_id=pallet.current_station_id)
    db.commit()
    route_rows = db.query(models.PalletStationRoute).filter_by(pallet_id=pallet_id).order_by(models.PalletStationRoute.sequence_no.asc()).all()
    component_station_rollup = build_station_component_rollup(db, pallet.id)
    events = db.query(models.PalletEvent).filter_by(pallet_id=pallet_id).order_by(models.PalletEvent.recorded_at.asc()).all()
    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    available_bins = get_available_pallet_bins(db, include_bin_id=pallet.storage_bin_id)
    return templates.TemplateResponse("pallet_detail.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "pallet": pallet, "part_rows": part_rows, "route_rows": route_rows, "component_station_rollup": component_station_rollup, "events": events, "stations": stations, "available_bins": available_bins, "station_label": station_label, "location_label": pallet_location_label(db, pallet), "errors": {}})


@app.get("/production/pallet/{pallet_id}/edit", response_class=HTMLResponse)
def pallet_edit(pallet_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = db.query(models.Pallet).filter_by(id=pallet_id).first()
    if not pallet:
        raise HTTPException(404)

    component_rows = get_pallet_part_rows(db, pallet)

    mpf = db.query(models.MpfMaster).filter_by(id=pallet.mpf_master_id).first() if pallet.mpf_master_id else None
    raw_material = None
    if mpf:
        parsed_sheet = parse_sheet_size(mpf.sheet_size)
        if parsed_sheet:
            width, length = parsed_sheet
            raw_material = db.query(models.RawMaterial).filter(
                models.RawMaterial.gauge == mpf.material,
                ((models.RawMaterial.width == width) & (models.RawMaterial.length == length)) |
                ((models.RawMaterial.width == length) & (models.RawMaterial.length == width)),
            ).first()

    return templates.TemplateResponse("pallet_edit.html", {
        "request": request,
        "user": user,
        "top_nav": TOP_NAV,
        "entity_groups": ENTITY_GROUPS,
        "pallet": pallet,
        "part_rows": component_rows,
        "raw_material_id": raw_material.id if raw_material else "-",
    })


@app.post("/production/pallet/{pallet_id}/edit")
async def pallet_edit_save(pallet_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = db.query(models.Pallet).filter_by(id=pallet_id).first()
    if not pallet:
        raise HTTPException(404)

    form = await request.form()
    component_ids = form.getlist("component_id")
    expected_qtys = form.getlist("expected_qty")
    qty_neededs = form.getlist("qty_needed")
    scrap_qtys = form.getlist("scrap_qty")
    current_qtys = form.getlist("current_qty")

    component_snapshot: list[dict] = []
    db.query(models.PalletPart).filter_by(pallet_id=pallet.id).delete(synchronize_session=False)

    frame_revision = ensure_current_part_revision(db, pallet.frame_part_number, user.username) if pallet.frame_part_number else None
    if frame_revision:
        db.add(models.PalletPart(
            pallet_id=pallet.id,
            part_revision_id=frame_revision.id,
            planned_quantity=pallet.expected_quantity,
            external_quantity_needed=0,
            actual_quantity=0,
            scrap_quantity=0,
        ))

    for index, component_id_raw in enumerate(component_ids):
        component_id = (component_id_raw or "").strip()
        if not component_id or component_id == (pallet.frame_part_number or ""):
            continue
        expected_qty = float(expected_qtys[index] or 0) if index < len(expected_qtys) else 0
        qty_needed = float(qty_neededs[index] or 0) if index < len(qty_neededs) else 0
        scrap_qty = float(scrap_qtys[index] or 0) if index < len(scrap_qtys) else 0
        current_qty = float(current_qtys[index] or 0) if index < len(current_qtys) else 0
        ensure_inventory_component_exists(db, component_id)
        component_revision = ensure_current_part_revision(db, component_id, user.username)
        db.add(models.PalletPart(
            pallet_id=pallet.id,
            part_revision_id=component_revision.id,
            planned_quantity=expected_qty,
            external_quantity_needed=qty_needed,
            actual_quantity=current_qty,
            scrap_quantity=scrap_qty,
        ))
        component_snapshot.append({
            "component_id": component_id,
            "expected_quantity": expected_qty,
            "qty_needed": qty_needed,
        })

    pallet.component_list_json = json.dumps(component_snapshot)
    db.commit()
    return RedirectResponse(f"/production/pallet/{pallet.id}/edit", status_code=302)


@app.post("/production/pallet/{pallet_id}/move")
def pallet_move(
    pallet_id: int,
    destination_type: str = Form("station"),
    destination_id: int | None = Form(None),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    pallet = db.query(models.Pallet).filter_by(id=pallet_id).first()
    if not pallet:
        raise HTTPException(404)

    if destination_type == "storage_bin":
        if not destination_id:
            raise HTTPException(422, "Storage bin is required")
        storage_bin = db.query(models.StorageBin).filter_by(id=destination_id).first()
        if not storage_bin:
            raise HTTPException(404, "Storage bin not found")
        location = db.query(models.StorageLocation).filter_by(id=storage_bin.storage_location_id).first()
        if not location or not location.pallet_storage:
            raise HTTPException(422, "Selected bin is not a pallet storage location")
        occupied_by_other = (storage_bin.component_id or "").strip() and storage_bin.id != pallet.storage_bin_id
        if occupied_by_other:
            raise HTTPException(422, "Selected bin is occupied")
        assign_pallet_to_storage_bin(db, pallet, storage_bin)
        pallet.status = "staged"
        location_note = f"Moved to {location.location_description} S{storage_bin.shelf_id} B{storage_bin.bin_id}"
        db.add(models.PalletEvent(pallet_id=pallet.id, station_id=None, event_type="moved", quantity=0, recorded_by=user.username, notes=notes or location_note))
    else:
        if not destination_id:
            raise HTTPException(422, "Station is required")
        station = db.query(models.Station).filter_by(id=destination_id).first()
        if not station:
            raise HTTPException(404)
        clear_pallet_storage_bin(db, pallet)
        pallet.storage_bin_id = None
        pallet.current_station_id = destination_id
        pallet.status = "in_progress"
        db.add(models.PalletEvent(pallet_id=pallet.id, station_id=destination_id, event_type="moved", quantity=0, recorded_by=user.username, notes=notes or f"Moved to {station.station_name}"))

    db.commit()
    return RedirectResponse(f"/production/pallet/{pallet.id}", status_code=302)


@app.post("/production/pallet/{pallet_id}/release")
def pallet_release_to_hk_queue(pallet_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = db.query(models.Pallet).filter_by(id=pallet_id).first()
    if not pallet:
        raise HTTPException(404)

    ensure_pallet_station_routing(db, pallet)
    first_route = db.query(models.PalletStationRoute).filter_by(pallet_id=pallet.id).order_by(models.PalletStationRoute.sequence_no.asc()).first()
    if not first_route or not first_route.station_id:
        raise HTTPException(422, "Pallet has no station route configured")

    already_released = db.query(models.PalletEvent).filter_by(pallet_id=pallet.id, event_type="released_to_queue").first()
    if already_released:
        raise HTTPException(422, "Pallet already released")

    clear_pallet_storage_bin(db, pallet)
    pallet.storage_bin_id = None
    pallet.current_station_id = None
    pallet.status = "queued"

    route_rows = db.query(models.PalletStationRoute).filter_by(pallet_id=pallet.id).order_by(models.PalletStationRoute.sequence_no.asc()).all()
    for route_row in route_rows:
        route_row.qty_completed = 0
        route_row.qty_scrap = 0
        route_row.status = "staged"
        route_row.location_id = "00"

    first_route.status = "queued"
    first_route.location_id = f"Q{first_route.station_id}"
    queue_pallet_for_station(db, pallet, first_route.station_id)

    update_inventory_for_released_pallet(db, pallet)
    db.add(models.PalletEvent(
        pallet_id=pallet.id,
        station_id=first_route.station_id,
        event_type="released_to_queue",
        quantity=pallet.expected_quantity or 0,
        recorded_by=user.username,
        notes=f"Released pallet to Station {first_route.station_id} queue",
    ))
    db.commit()
    return RedirectResponse(f"/production/pallet/{pallet.id}", status_code=302)


@app.post("/production/pallet/{pallet_id}/delete")
def production_pallet_delete(pallet_id: int, redirect_to: str = Form("/production?tab=active"), db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = db.query(models.Pallet).filter_by(id=pallet_id).first()
    if not pallet:
        raise HTTPException(404)

    linked_order = db.query(models.ProductionOrder).filter_by(id=pallet.production_order_id).first() if pallet.production_order_id else None

    clear_pallet_storage_bin(db, pallet)
    rollback_inventory_for_deleted_pallet(db, pallet)

    db.query(models.Queue).filter_by(pallet_id=pallet_id).delete(synchronize_session=False)
    db.query(models.PalletEvent).filter_by(pallet_id=pallet_id).delete(synchronize_session=False)
    db.query(models.PalletPart).filter_by(pallet_id=pallet_id).delete(synchronize_session=False)
    db.query(models.PalletStationRoute).filter_by(pallet_id=pallet_id).delete(synchronize_session=False)
    db.query(models.PalletRevision).filter_by(pallet_id=pallet_id).delete(synchronize_session=False)

    if linked_order:
        pallet.production_order_id = None
        db.delete(linked_order)

    db.delete(pallet)
    db.commit()
    return RedirectResponse(redirect_to if redirect_to.startswith("/") else "/production?tab=active", status_code=302)


@app.post("/production/create-pallet")
def production_create_pallet(part_revision_id: int = Form(...), quantity: float = Form(...), location_station_id: int | None = Form(None), db: Session = Depends(get_db), user=Depends(require_login)):
    if quantity <= 0:
        raise HTTPException(422, "Quantity must be greater than zero")
    code = f"P-{int(datetime.utcnow().timestamp())}"
    pallet = models.Pallet(pallet_code=code, pallet_type="manual", status="staged", current_station_id=location_station_id, created_by=user.username)
    db.add(pallet)
    db.commit()
    db.refresh(pallet)
    db.add(models.PalletPart(pallet_id=pallet.id, part_revision_id=part_revision_id, planned_quantity=quantity, actual_quantity=quantity, scrap_quantity=0))
    ensure_pallet_station_routing(db, pallet, fallback_station_id=location_station_id)
    db.add(models.PalletEvent(pallet_id=pallet.id, station_id=location_station_id, event_type="created", quantity=quantity, recorded_by=user.username, notes="Manual pallet creation"))
    db.commit()
    create_traveler_file(db, pallet.id)
    return RedirectResponse(f"/production/pallet/{pallet.id}", status_code=302)


@app.post("/production/create-order")
def production_create_order(
    frame_part_id: str = Form(...),
    mpf_master_id: int = Form(...),
    expected_quantity: float = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    try:
        mpf = db.query(models.MpfMaster).filter_by(id=mpf_master_id).first()
        if not mpf or mpf.part_id != frame_part_id:
            raise HTTPException(422, "Invalid HK MPF selection")
        if mpf.qty_produced <= 0:
            raise HTTPException(422, "Selected HK MPF has no qty produced value")
        if expected_quantity <= 0:
            raise HTTPException(422, "Expected quantity must be greater than zero")

        sheet_count = expected_quantity / mpf.qty_produced
        if abs(round(sheet_count) - sheet_count) > 1e-6:
            raise HTTPException(422, "Expected quantity must be a multiple of qty produced")
        sheet_count = float(round(sheet_count))

        if not db.query(models.Part).filter_by(part_number=frame_part_id).first() and not db.query(models.PartMaster).filter_by(part_id=frame_part_id).first():
            raise HTTPException(422, f"Part {frame_part_id} is not defined in parts or engineering part tables")
        revision = ensure_current_part_revision(db, frame_part_id, user.username)

        parsed_sheet = parse_sheet_size(mpf.sheet_size)
        material_row = None
        if parsed_sheet:
            width, length = parsed_sheet
            material_row = db.query(models.RawMaterial).filter(
                models.RawMaterial.gauge == mpf.material,
                ((models.RawMaterial.width == width) & (models.RawMaterial.length == length)) |
                ((models.RawMaterial.width == length) & (models.RawMaterial.length == width)),
            ).first()

        if material_row and material_row.qty_on_hand < sheet_count:
            raise HTTPException(422, "Not enough raw material sheets on hand")

        order = models.ProductionOrder(part_revision_id=revision.id, quantity_ordered=expected_quantity, status="planned")
        db.add(order)
        db.flush()

        route_stations = get_part_station_routes(db, frame_part_id)
        first_station = route_stations[0] if route_stations else db.query(models.Station).filter_by(active=True).order_by(models.Station.id.asc()).first()
        pallet = models.Pallet(
            pallet_code="",
            pallet_type="manual",
            production_order_id=order.id,
            mpf_master_id=mpf.id,
            frame_part_number=frame_part_id,
            expected_quantity=expected_quantity,
            sheet_count=sheet_count,
            status="staged",
            current_station_id=None,
            created_by=user.username,
        )
        db.add(pallet)
        db.flush()
        pallet.pallet_code = f"P-{int(datetime.utcnow().timestamp())}-{order.id}"

        empty_storage_bin = next(iter(get_available_pallet_bins(db, exclude_hk=True)), None)
        if not empty_storage_bin:
            empty_storage_bin = next(iter(get_available_pallet_bins(db)), None)
        if not empty_storage_bin:
            raise HTTPException(422, "No available pallet storage bins found for new orders")
        assign_pallet_to_storage_bin(db, pallet, empty_storage_bin)

        component_snapshot = build_component_quantities(db, frame_part_id, expected_quantity, sheet_count, mpf.id)
        all_part_revisions = [
            {
                "part_revision_id": revision.id,
                "planned_quantity": expected_quantity,
                "external_quantity_needed": 0,
            }
        ]

        for component in component_snapshot:
            component_id = component["component_id"]
            qty_needed = float(component.get("qty_needed") or 0)
            expected_component_qty = float(component.get("expected_quantity") or 0)
            component_revision = ensure_current_part_revision(db, component_id, user.username)
            all_part_revisions.append({
                "part_revision_id": component_revision.id,
                "planned_quantity": expected_component_qty,
                "external_quantity_needed": qty_needed,
            })

        pallet.component_list_json = json.dumps(component_snapshot)

        for part_item in all_part_revisions:
            db.add(models.PalletPart(
                pallet_id=pallet.id,
                part_revision_id=part_item["part_revision_id"],
                planned_quantity=part_item["planned_quantity"],
                external_quantity_needed=part_item.get("external_quantity_needed", 0),
                actual_quantity=0,
                scrap_quantity=0,
            ))
        db.add(models.PalletRevision(
            pallet_id=pallet.id,
            revision_code="R1",
            snapshot_json=json.dumps({
                "frame_part_number": frame_part_id,
                "expected_quantity": expected_quantity,
                "sheet_count": sheet_count,
                "components": component_snapshot,
            }),
            created_by=user.username,
        ))
        db.add(models.PalletEvent(
            pallet_id=pallet.id,
            station_id=first_station.id if first_station else None,
            event_type="created",
            quantity=expected_quantity,
            recorded_by=user.username,
            notes=f"Order created from HK MPF {mpf.mpf_filename} and staged to pallet storage bin {empty_storage_bin.id}",
        ))

        route_station_ids: list[int] = []
        seen_station_ids: set[int] = set()
        for route_station in route_stations:
            if route_station.id in seen_station_ids:
                continue
            route_station_ids.append(route_station.id)
            seen_station_ids.add(route_station.id)

        if not route_station_ids:
            route_station_ids = [station.id for station in db.query(models.Station).filter_by(active=True).order_by(models.Station.id.asc()).all()]

        ensure_pallet_station_routing(db, pallet, fallback_station_id=first_station.id if first_station else None)

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(500, f"Failed to create order and pallet: {exc}")

    create_traveler_file(db, pallet.id)
    return RedirectResponse(f"/production/pallet/{pallet.id}", status_code=302)


@app.get("/engineering", response_class=HTMLResponse)
def engineering_dashboard(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    open_questions = db.query(models.EngineeringQuestion).filter_by(status="open").order_by(models.EngineeringQuestion.created_at.desc()).limit(30).all()
    latest_files = db.query(models.PartRevisionFile).order_by(models.PartRevisionFile.uploaded_at.desc()).limit(20).all()
    return templates.TemplateResponse("engineering_dashboard.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "open_questions": open_questions, "latest_files": latest_files, **engineering_nav_context()})


@app.get("/engineering/parts", response_class=HTMLResponse)
def engineering_parts_page(request: Request, page: int = 1, mode: str = "", db: Session = Depends(get_db), user=Depends(require_login)):
    page_size = 50
    page = max(page, 1)
    total_parts = db.query(models.PartMaster).count()
    parts = db.query(models.PartMaster).order_by(models.PartMaster.part_id.asc()).offset((page - 1) * page_size).limit(page_size).all()
    return templates.TemplateResponse("engineering_parts.html", {
        "request": request,
        "user": user,
        "top_nav": TOP_NAV,
        "entity_groups": ENTITY_GROUPS,
        "parts": parts,
        "page": page,
        "page_size": page_size,
        "total_parts": total_parts,
        "show_add": mode == "add",
        **engineering_nav_context(),
    })


@app.post("/engineering/parts")
def engineering_parts_create(part_id: str = Form(...), description: str = Form(""), db: Session = Depends(get_db), user=Depends(require_login)):
    clean_part_id = part_id.strip()
    if not clean_part_id:
        raise HTTPException(422, "part_id is required")
    existing = db.query(models.PartMaster).filter_by(part_id=clean_part_id).first()
    if existing:
        raise HTTPException(422, "part_id already exists")
    db.add(models.PartMaster(part_id=clean_part_id, description=description.strip(), cur_rev=0))
    db.commit()
    return RedirectResponse(f"/engineering/parts/{clean_part_id}?mode=edit", status_code=302)


@app.post("/engineering/parts/{part_id}/delete")
def engineering_part_delete(part_id: str, db: Session = Depends(get_db), user=Depends(require_login)):
    part = db.query(models.PartMaster).filter_by(part_id=part_id).first()
    if not part:
        raise HTTPException(404)

    db.query(models.RevisionBom).filter_by(part_id=part_id).delete(synchronize_session=False)
    db.query(models.RevisionHeader).filter_by(part_id=part_id).delete(synchronize_session=False)
    db.delete(part)
    db.commit()
    return RedirectResponse("/engineering/parts", status_code=302)


@app.get("/engineering/parts/{part_id}", response_class=HTMLResponse)
def engineering_part_detail(part_id: str, request: Request, mode: str = "edit", rev_id: int | None = None, db: Session = Depends(get_db), user=Depends(require_login)):
    part = db.query(models.PartMaster).filter_by(part_id=part_id).first()
    if not part:
        raise HTTPException(404)
    mode = "edit"
    selected_rev = rev_id if rev_id is not None else part.cur_rev
    bom_lines = db.query(models.RevisionBom).filter_by(part_id=part_id, rev_id=selected_rev).order_by(models.RevisionBom.id.asc()).all()
    revision_header = db.query(models.RevisionHeader).filter_by(part_id=part_id, rev_id=selected_rev).first()
    revision_list = db.query(models.RevisionHeader.rev_id).filter_by(part_id=part_id).order_by(models.RevisionHeader.rev_id.desc()).all()

    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    assigned_routes = db.query(models.PartStationRoute).filter_by(part_id=part_id).order_by(models.PartStationRoute.route_order.asc(), models.PartStationRoute.id.asc()).all()
    assigned_station_ids = [route.station_id for route in assigned_routes]
    station_map = {station.id: station for station in stations}
    assigned_stations = [station_map[route.station_id] for route in assigned_routes if route.station_id in station_map]
    available_stations = [station for station in stations if station.id not in assigned_station_ids]

    revision_file_buttons: list[dict[str, str]] = []
    if revision_header:
        dwg_payload: dict[str, str] = {}
        if revision_header.weld_mod and revision_header.weld_mod.strip().startswith("{"):
            try:
                decoded = json.loads(revision_header.weld_mod)
                if isinstance(decoded, dict):
                    dwg_payload = {str(key): str(value) for key, value in decoded.items() if isinstance(value, str)}
            except json.JSONDecodeError:
                dwg_payload = {}

        button_specs = [
            ("HK PDF", revision_header.hk_file),
            ("HK MPF", revision_header.cut_dwg),
            ("OMAX PDF", revision_header.wj_file),
            ("OMAX G", revision_header.fab_pdf),
            ("Brake Press PDF", revision_header.cut_pdf),
            ("Brake Press DWG", dwg_payload.get("brake_dwg", "")),
            ("WELD PDF", revision_header.weld_pdf),
            ("WELD DWG", dwg_payload.get("weld_dwg", "")),
            ("WELD MOD", revision_header.weld_dwg),
        ]

        for index, (label, stored_path) in enumerate(button_specs):
            if not stored_path:
                continue
            file_path = Path(stored_path)
            if not file_path.exists() or not file_path.is_file():
                continue
            revision_file_buttons.append({
                "label": label,
                "href": f"/engineering/parts/{part_id}/revisions/{selected_rev}/files/{index}",
            })

    return templates.TemplateResponse("engineering_part_detail.html", {
        "request": request,
        "user": user,
        "top_nav": TOP_NAV,
        "entity_groups": ENTITY_GROUPS,
        "part": part,
        "selected_rev": selected_rev,
        "bom_lines": bom_lines,
        "revision_header": revision_header,
        "mode": mode,
        "revision_list": [item[0] for item in revision_list],
        "revision_file_buttons": revision_file_buttons,
        "assigned_stations": assigned_stations,
        "available_stations": available_stations,
        **engineering_nav_context(),
    })


@app.post("/engineering/parts/{part_id}/station-routing")
async def engineering_part_station_routing(part_id: str, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    part = db.query(models.PartMaster).filter_by(part_id=part_id).first()
    if not part:
        raise HTTPException(404)

    form = await request.form()
    station_ids_payload = (form.get("station_ids") or "").strip()
    station_ids: list[int] = []
    for value in station_ids_payload.split(","):
        cleaned = value.strip()
        if cleaned.isdigit():
            station_ids.append(int(cleaned))

    db.query(models.PartStationRoute).filter_by(part_id=part_id).delete(synchronize_session=False)
    for index, station_id in enumerate(station_ids, start=1):
        station = db.query(models.Station).filter_by(id=station_id, active=True).first()
        if not station:
            continue
        db.add(models.PartStationRoute(part_id=part_id, station_id=station_id, route_order=index))

    db.commit()
    return RedirectResponse(f"/engineering/parts/{part_id}?mode=edit", status_code=302)


@app.get("/engineering/parts/{part_id}/revisions/{rev_id}/files/{file_key}")
def engineering_part_download_revision_file(part_id: str, rev_id: int, file_key: int, db: Session = Depends(get_db), user=Depends(require_login)):
    header = db.query(models.RevisionHeader).filter_by(part_id=part_id, rev_id=max(rev_id, 0)).first()
    if not header:
        raise HTTPException(404)

    dwg_payload: dict[str, str] = {}
    if header.weld_mod and header.weld_mod.strip().startswith("{"):
        try:
            decoded = json.loads(header.weld_mod)
            if isinstance(decoded, dict):
                dwg_payload = {str(key): str(value) for key, value in decoded.items() if isinstance(value, str)}
        except json.JSONDecodeError:
            dwg_payload = {}

    file_paths = [
        header.hk_file,
        header.cut_dwg,
        header.wj_file,
        header.fab_pdf,
        header.cut_pdf,
        dwg_payload.get("brake_dwg", ""),
        header.weld_pdf,
        dwg_payload.get("weld_dwg", ""),
        header.weld_dwg,
    ]
    if file_key < 0 or file_key >= len(file_paths):
        raise HTTPException(404)

    stored_path = file_paths[file_key]
    if not stored_path:
        raise HTTPException(404)

    file_path = Path(stored_path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404)

    return FileResponse(path=file_path, filename=file_path.name)


@app.post("/engineering/parts/{part_id}/update")
def engineering_part_update(part_id: str, description: str = Form(""), cur_rev: int = Form(...), db: Session = Depends(get_db), user=Depends(require_login)):
    part = db.query(models.PartMaster).filter_by(part_id=part_id).first()
    if not part:
        raise HTTPException(404)
    previous_rev = part.cur_rev
    part.description = description.strip()
    part.cur_rev = max(cur_rev, 0)
    if part.cur_rev > previous_rev:
        existing_header = db.query(models.RevisionHeader).filter_by(part_id=part_id, rev_id=part.cur_rev).first()
        if not existing_header:
            db.add(models.RevisionHeader(part_id=part_id, rev_id=part.cur_rev))
    db.commit()
    return RedirectResponse(f"/engineering/parts/{part_id}?mode=edit", status_code=302)


@app.post("/engineering/parts/{part_id}/bom-lines")
def engineering_part_add_bom_line(part_id: str, rev_id: int = Form(...), comp_id: str = Form(...), comp_qty: float = Form(...), db: Session = Depends(get_db), user=Depends(require_login)):
    part = db.query(models.PartMaster).filter_by(part_id=part_id).first()
    if not part:
        raise HTTPException(404)
    db.add(models.RevisionBom(part_id=part_id, rev_id=max(rev_id, 0), comp_id=comp_id.strip(), comp_qty=comp_qty))
    db.commit()
    return RedirectResponse(f"/engineering/parts/{part_id}?mode=edit&rev_id={max(rev_id, 0)}", status_code=302)


@app.post("/engineering/parts/{part_id}/revision-header")
async def engineering_part_upsert_revision_header(
    part_id: str,
    rev_id: int = Form(...),
    hk_qty: float = Form(0),
    wj_qty: float = Form(0),
    released_date: str = Form(""),
    released_by: str = Form(""),
    release_comment: str = Form(""),
    hk_pdf_upload: UploadFile | None = File(None),
    wj_pdf_upload: UploadFile | None = File(None),
    brake_pdf_upload: UploadFile | None = File(None),
    weld_pdf_upload: UploadFile | None = File(None),
    hk_machine_upload: UploadFile | None = File(None),
    wj_machine_upload: UploadFile | None = File(None),
    brake_machine_upload: UploadFile | None = File(None),
    weld_machine_upload: UploadFile | None = File(None),
    brake_dwg_upload: UploadFile | None = File(None),
    weld_dwg_upload: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    part = db.query(models.PartMaster).filter_by(part_id=part_id).first()
    if not part:
        raise HTTPException(404)

    header = db.query(models.RevisionHeader).filter_by(part_id=part_id, rev_id=max(rev_id, 0)).first()
    if not header:
        header = models.RevisionHeader(part_id=part_id, rev_id=max(rev_id, 0))
        db.add(header)

    async def maybe_store_upload(upload: UploadFile | None) -> str | None:
        if not upload or not upload.filename:
            return None
        safe_name = Path(upload.filename).name
        stored_name = f"pm_{part_id}_r{max(rev_id, 0)}_{int(datetime.utcnow().timestamp())}_{safe_name}"
        out_path = PART_FILE_DIR / stored_name
        data = await upload.read()
        out_path.write_bytes(data)
        return str(out_path)

    hk_pdf_path = await maybe_store_upload(hk_pdf_upload)
    wj_pdf_path = await maybe_store_upload(wj_pdf_upload)
    brake_pdf_path = await maybe_store_upload(brake_pdf_upload)
    weld_pdf_path = await maybe_store_upload(weld_pdf_upload)
    hk_machine_path = await maybe_store_upload(hk_machine_upload)
    wj_machine_path = await maybe_store_upload(wj_machine_upload)
    brake_machine_path = await maybe_store_upload(brake_machine_upload)
    weld_machine_path = await maybe_store_upload(weld_machine_upload)
    brake_dwg_path = await maybe_store_upload(brake_dwg_upload)
    weld_dwg_path = await maybe_store_upload(weld_dwg_upload)

    if hk_pdf_path:
        header.hk_file = hk_pdf_path
    header.hk_qty = hk_qty
    if wj_pdf_path:
        header.wj_file = wj_pdf_path
    header.wj_qty = wj_qty
    if brake_pdf_path:
        header.cut_pdf = brake_pdf_path
    if weld_pdf_path:
        header.weld_pdf = weld_pdf_path
    if hk_machine_path:
        header.cut_dwg = hk_machine_path
    if wj_machine_path:
        header.fab_pdf = wj_machine_path
    if brake_machine_path:
        header.fab_dwg = brake_machine_path
    if weld_machine_path:
        header.weld_dwg = weld_machine_path

    dwg_payload = {}
    try:
        if header.weld_mod.strip().startswith("{"):
            dwg_payload = json.loads(header.weld_mod)
            if not isinstance(dwg_payload, dict):
                dwg_payload = {}
    except json.JSONDecodeError:
        dwg_payload = {}
    if brake_dwg_path:
        dwg_payload["brake_dwg"] = brake_dwg_path
    if weld_dwg_path:
        dwg_payload["weld_dwg"] = weld_dwg_path
    header.weld_mod = json.dumps(dwg_payload) if dwg_payload else header.weld_mod

    header.released_by = released_by.strip()
    header.release_comment = release_comment.strip()
    if released_date.strip():
        header.released_date = datetime.fromisoformat(f"{released_date.strip()}T00:00:00")
    else:
        header.released_date = None
    db.commit()
    return RedirectResponse(f"/engineering/parts/{part_id}?mode=edit&rev_id={max(rev_id, 0)}", status_code=302)


@app.get("/engineering/revisions/{part_revision_id}/files", response_class=HTMLResponse)
def engineering_revision_files(part_revision_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    part_revision = db.query(models.PartRevision).filter_by(id=part_revision_id).first()
    if not part_revision:
        raise HTTPException(404)
    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    files = db.query(models.PartRevisionFile).filter_by(part_revision_id=part_revision_id).order_by(models.PartRevisionFile.uploaded_at.desc()).all()
    return templates.TemplateResponse("engineering_upload.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "part_revision": part_revision, "stations": stations, "files": files, "message": None, "error": None})


@app.post("/engineering/revisions/{part_revision_id}/files", response_class=HTMLResponse)
async def engineering_revision_files_save(part_revision_id: int, request: Request, file_type: str = Form(...), available_station_ids: list[int] = Form([]), upload_file: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(require_login)):
    part_revision = db.query(models.PartRevision).filter_by(id=part_revision_id).first()
    if not part_revision:
        raise HTTPException(404)

    allowed_types = {"laser", "waterjet", "welder_module", "drawing", "pdf"}
    if file_type not in allowed_types:
        raise HTTPException(422, "Invalid file type")

    safe_name = Path(upload_file.filename or "upload.dat").name
    stored_name = f"pr{part_revision_id}_{int(datetime.utcnow().timestamp())}_{safe_name}"
    out_path = PART_FILE_DIR / stored_name
    data = await upload_file.read()
    out_path.write_bytes(data)

    station_csv = ",".join(str(sid) for sid in sorted(set(available_station_ids)))
    db.add(models.PartRevisionFile(part_revision_id=part_revision_id, file_type=file_type, original_name=safe_name, stored_path=str(out_path), station_ids_csv=station_csv, uploaded_by=user.username))

    process = db.query(models.PartProcessDefinition).filter_by(part_revision_id=part_revision_id).first()
    if not process:
        process = models.PartProcessDefinition(part_revision_id=part_revision_id)
        db.add(process)

    if file_type == "laser":
        process.laser_required = True
        process.laser_program_path = str(out_path)
    elif file_type == "waterjet":
        process.waterjet_required = True
        process.waterjet_program_path = str(out_path)
    elif file_type == "welder_module":
        process.robotic_weld_required = True
        process.robotic_weld_program_path = str(out_path)
    else:
        process.manual_weld_required = True
        process.manual_weld_drawing_path = str(out_path)

    db.commit()
    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    files = db.query(models.PartRevisionFile).filter_by(part_revision_id=part_revision_id).order_by(models.PartRevisionFile.uploaded_at.desc()).all()
    return templates.TemplateResponse("engineering_upload.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "part_revision": part_revision, "stations": stations, "files": files, "message": "Revision file uploaded and station access set.", "error": None})


@app.get("/engineering/add-machine-program", response_class=HTMLResponse)
def engineering_machine_program_stub(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return templates.TemplateResponse("engineering_machine_program_stub.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, **engineering_nav_context()})


@app.get("/engineering/hk-mpfs", response_class=HTMLResponse)
def engineering_hk_mpfs_page(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    rows = db.query(models.MpfMaster).order_by(models.MpfMaster.created_at.desc()).all()
    return templates.TemplateResponse("engineering_hk_mpfs.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "rows": rows, **engineering_nav_context()})


@app.get("/engineering/hk-mpfs/{mpf_id}", response_class=HTMLResponse)
def engineering_hk_mpf_detail_page(mpf_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    record = db.query(models.MpfMaster).filter_by(id=mpf_id).first()
    if not record:
        raise HTTPException(404)
    details = db.query(models.MpfDetail).filter_by(mpf_master_id=mpf_id).order_by(models.MpfDetail.id.asc()).all()
    return templates.TemplateResponse("engineering_hk_mpf_detail.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "record": record, "details": details, **engineering_nav_context()})


@app.post("/engineering/hk-mpfs/{mpf_id}/edit")
async def engineering_hk_mpf_edit(mpf_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    record = db.query(models.MpfMaster).filter_by(id=mpf_id).first()
    if not record:
        raise HTTPException(404)
    form = await request.form()
    if "part_id" in form:
        record.part_id = (form.get("part_id") or "").strip()
    record.description = (form.get("description") or "").strip()
    record.qty_produced = float(form.get("qty_produced") or 0)
    record.material = (form.get("material") or "").strip()
    db.commit()
    return RedirectResponse("/engineering/hk-mpfs", status_code=302)


@app.post("/engineering/hk-mpfs/{mpf_id}/delete")
def engineering_hk_mpf_delete(mpf_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    record = db.query(models.MpfMaster).filter_by(id=mpf_id).first()
    if record:
        db.query(models.MpfDetail).filter_by(mpf_master_id=mpf_id).delete(synchronize_session=False)
        db.query(models.EngineeringPdf).filter_by(mpf_master_id=mpf_id).update({"mpf_master_id": None}, synchronize_session=False)
        db.delete(record)
        db.commit()
    return RedirectResponse("/engineering/hk-mpfs", status_code=302)


@app.post("/engineering/hk-mpfs/{mpf_id}/details")
async def engineering_hk_mpf_add_detail(mpf_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    record = db.query(models.MpfMaster).filter_by(id=mpf_id).first()
    if not record:
        raise HTTPException(404)
    form = await request.form()
    component_id = (form.get("component_id") or "").strip()
    if component_id:
        db.add(models.MpfDetail(
            mpf_master_id=mpf_id,
            sheet_qty=float(form.get("sheet_qty") or 0),
            assy_qty=float(form.get("assy_qty") or 0),
            component_id=component_id,
        ))
        ensure_inventory_component_exists(db, component_id)
        db.commit()
    return RedirectResponse(f"/engineering/hk-mpfs/{mpf_id}", status_code=302)


@app.post("/engineering/hk-mpfs/{mpf_id}/details/{detail_id}/edit")
async def engineering_hk_mpf_edit_detail(mpf_id: int, detail_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    detail = db.query(models.MpfDetail).filter_by(id=detail_id, mpf_master_id=mpf_id).first()
    if not detail:
        raise HTTPException(404)
    form = await request.form()
    component_id = (form.get("component_id") or "").strip()
    detail.sheet_qty = float(form.get("sheet_qty") or 0)
    detail.assy_qty = float(form.get("assy_qty") or 0)
    detail.component_id = component_id
    if component_id:
        ensure_inventory_component_exists(db, component_id)
    db.commit()
    return RedirectResponse(f"/engineering/hk-mpfs/{mpf_id}", status_code=302)


@app.post("/engineering/hk-mpfs/{mpf_id}/details/{detail_id}/delete")
def engineering_hk_mpf_delete_detail(mpf_id: int, detail_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    detail = db.query(models.MpfDetail).filter_by(id=detail_id, mpf_master_id=mpf_id).first()
    if detail:
        db.delete(detail)
        db.commit()
    return RedirectResponse(f"/engineering/hk-mpfs/{mpf_id}", status_code=302)


def parse_hk_cutsheet(pdf_bytes: bytes) -> dict:
    try:
        from io import BytesIO
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - surfaced to UI
        raise HTTPException(status_code=500, detail="PDF parser dependency is not installed.") from exc

    text_pages: list[str] = []
    layout_pages: list[str] = []
    reader = PdfReader(BytesIO(pdf_bytes))
    for page in reader.pages:
        text_pages.append(page.extract_text() or "")
        layout_pages.append(page.extract_text(extraction_mode="layout") or "")

    page1 = text_pages[0] if text_pages else ""
    layout_page1 = layout_pages[0] if layout_pages else ""
    page2 = text_pages[1] if len(text_pages) > 1 else ""
    all_text = "\n".join(text_pages)

    primary_part_id = ""
    primary_description = ""
    dwg_match = re.search(r"DWG\s*#\s*[:\-]?\s*([A-Z0-9\-_.]+)", page2, re.IGNORECASE)
    if dwg_match:
        primary_part_id = dwg_match.group(1).strip()

    if page2:
        page2_lines = [" ".join(line.split()) for line in page2.splitlines() if line.strip()]
        for i, line in enumerate(page2_lines):
            if re.search(r"DWG\s*#", line, re.IGNORECASE):
                if i + 1 < len(page2_lines):
                    descriptor_line = page2_lines[i + 1]
                    if primary_part_id and descriptor_line.upper().startswith(primary_part_id.upper()):
                        primary_description = descriptor_line[len(primary_part_id):].strip(" -:")
                    elif descriptor_line:
                        primary_description = descriptor_line.strip()
                break

    qty_produced = 0
    makes_match = re.search(r"make(?:s)?\s+(\d+)\s+frames?", all_text, re.IGNORECASE)
    if makes_match:
        qty_produced = int(makes_match.group(1))

    sheet_size = ""
    sheet_size_patterns = [
        r"Material\s+size\s*[:\-]?\s*([0-9.]+\s*[xX]\s*[0-9.]+(?:\s*[xX]\s*[0-9.]+)?)",
        r"Sheet\s+size\s*[:\-]?\s*([0-9.]+\s*[xX]\s*[0-9.]+(?:\s*[xX]\s*[0-9.]+)?)",
        r"\b([0-9.]+\s*[xX]\s*[0-9.]+\s*[xX]\s*[0-9.]+)\b",
    ]
    for pattern in sheet_size_patterns:
        size_match = re.search(pattern, all_text, re.IGNORECASE)
        if not size_match:
            continue
        sheet_size = re.sub(r"\s*[xX]\s*", " x ", size_match.group(1)).strip()
        break

    material = ""
    material_match = re.search(r"\b(10|12|16)\s*ga\b", all_text, re.IGNORECASE)
    if material_match:
        material = f"{material_match.group(1)}ga"

    component_debug = _extract_hk_component_debug(page1)
    layout_component_debug = _extract_hk_component_debug(layout_page1)
    components = _parse_hk_components([page1, layout_page1], qty_produced)

    return {
        "primary_part_id": primary_part_id,
        "primary_description": primary_description,
        "qty_produced": qty_produced,
        "sheet_size": sheet_size,
        "material": material,
        "components": components,
        "debug": {
            "page1_component_lines": component_debug,
            "layout_page1_component_lines": layout_component_debug,
        },
    }


def _extract_hk_component_debug(page1_text: str) -> list[str]:
    lines = [" ".join(line.split()) for line in page1_text.splitlines() if line.strip()]
    start_index = 0
    for i, line in enumerate(lines):
        compact = re.sub(r"\s+", "", line.lower())
        if "part#" in compact and "#pcs" in compact:
            start_index = i + 1
            break

    end_index = len(lines)
    for i in range(start_index, len(lines)):
        lower = lines[i].lower()
        if "dwg#" in lower or lower.startswith("notes") or "date printed" in lower:
            end_index = i
            break

    return lines[start_index:end_index]


def _parse_hk_component_line(text: str) -> tuple[str, int | None] | None:
    if "FR-" not in text:
        return None

    row_prefix_match = re.match(r"^\s*\d{1,3}\b\s+", text)
    text_without_row = text[row_prefix_match.end():] if row_prefix_match else text

    match = re.search(r"(FR-[A-Z0-9]+)", text_without_row)
    if not match:
        return None

    component_token = match.group(1)
    component_id = component_token
    sheet_qty: int | None = None

    token_suffix_match = re.match(r"^(FR-(?:\d{5}[A-Z]?))(\d{1,2})$", component_token)
    if token_suffix_match:
        component_id = token_suffix_match.group(1)
        sheet_qty = int(token_suffix_match.group(2))

    start_match = re.match(r"^(FR-[A-Z0-9]*[A-Z]+)(\d{1,2})\b", text_without_row)
    if start_match and re.search(r"\d+\.\d+", text):
        component_id = start_match.group(1)
        sheet_qty = int(start_match.group(2))

    if sheet_qty is None and component_id[-1].isalpha():
        appended_match = re.search(re.escape(component_id) + r"(\d{1,2})\b", text_without_row)
        if appended_match:
            sheet_qty = int(appended_match.group(1))

    if sheet_qty is None:
        trailing_match = re.search(r"(?<!\.)(\d{1,2})\s*$", text_without_row)
        if trailing_match:
            sheet_qty = int(trailing_match.group(1))

    if sheet_qty is None:
        integers = re.findall(r"(?<!\.)\b\d{1,2}\b(?!\.)", text_without_row)
        if integers:
            sheet_qty = int(integers[-1])

    return component_id, sheet_qty


def _parse_hk_components(page_texts: list[str], qty_produced: int) -> list[dict]:
    seen: dict[str, dict] = {}

    candidate_lines: list[str] = []
    for text in page_texts:
        if text:
            candidate_lines.extend(_extract_hk_component_debug(text))

    parsed_with_qty = [
        _parse_hk_component_line(" ".join(line.split())) for line in candidate_lines
    ]
    parsed_with_qty = [parsed for parsed in parsed_with_qty if parsed and parsed[1] is not None]

    if not parsed_with_qty:
        for text in page_texts:
            if not text:
                continue
            fr_separated = text.replace("FR-", "\nFR-")
            fallback_lines = [
                line.strip()
                for line in fr_separated.splitlines()
                if line.strip().startswith("FR-")
            ]
            parsed_with_qty.extend(
                parsed
                for parsed in (
                    _parse_hk_component_line(" ".join(line.split()))
                    for line in fallback_lines
                )
                if parsed and parsed[1] is not None
            )

    for component_id, sheet_qty in parsed_with_qty:
        existing = seen.get(component_id)
        if existing and existing["sheet_qty"] >= sheet_qty:
            continue

        seen[component_id] = {
            "sheet_qty": sheet_qty,
            "assy_qty": round(sheet_qty / qty_produced, 4) if qty_produced else 0,
            "component_id": component_id,
        }

    return list(seen.values())


def ensure_inventory_component_exists(db: Session, component_id: str):
    existing_part = db.query(models.Part).filter_by(part_number=component_id).first()
    if existing_part:
        return
    new_part = models.Part(part_number=component_id, description="")
    db.add(new_part)
    db.flush()
    db.add(models.PartInventory(
        part_id=new_part.id,
        qty_on_hand_total=0,
        qty_stored=0,
        qty_queued_to_cut=0,
        qty_to_bend=0,
        qty_to_weld=0,
    ))


def upsert_mpf_master_with_details(db: Session, mpf_filename: str, parsed: dict, components: list[dict]) -> models.MpfMaster:
    record = db.query(models.MpfMaster).filter_by(mpf_filename=mpf_filename).first()
    if not record:
        record = models.MpfMaster(mpf_filename=mpf_filename)
        db.add(record)
        db.flush()
    record.part_id = (parsed.get("primary_part_id") or "").strip()
    record.description = (parsed.get("primary_description") or "").strip()
    record.qty_produced = float(parsed.get("qty_produced") or 0)
    record.material = (parsed.get("material") or "").strip()
    record.sheet_size = (parsed.get("sheet_size") or "").strip()

    db.query(models.MpfDetail).filter_by(mpf_master_id=record.id).delete(synchronize_session=False)
    for component in components:
        component_id = (component.get("component_id") or "").strip()
        if not component_id:
            continue
        db.add(models.MpfDetail(
            mpf_master_id=record.id,
            sheet_qty=float(component.get("sheet_qty") or 0),
            assy_qty=float(component.get("assy_qty") or 0),
            component_id=component_id,
        ))
    return record


def upsert_engineering_pdf(db: Session, pdf_filename: str, pdf_path: Path, mpf_master_id: int | None, hk_laser: bool, omax_wj: bool, bp: bool, welding: bool):
    pdf_record = db.query(models.EngineeringPdf).filter_by(pdf_filename=pdf_filename).first()
    if not pdf_record:
        pdf_record = models.EngineeringPdf(pdf_filename=pdf_filename, pdf_path=str(pdf_path))
        db.add(pdf_record)
    pdf_record.pdf_path = str(pdf_path)
    pdf_record.mpf_master_id = mpf_master_id
    pdf_record.hk_laser = hk_laser
    pdf_record.omax_wj = omax_wj
    pdf_record.bp = bp
    pdf_record.welding = welding


@app.post("/engineering/hk-mpfs/parse")
async def engineering_hk_mpf_parse(mpf_file: UploadFile = File(...), pdf_file: UploadFile = File(...), user=Depends(require_login)):
    if not mpf_file.filename:
        raise HTTPException(status_code=400, detail="MPF file is required.")
    if not pdf_file.filename or not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="PDF file is required.")

    pdf_bytes = await pdf_file.read()
    parsed = parse_hk_cutsheet(pdf_bytes)
    return JSONResponse(parsed)


@app.post("/engineering/parts/upload-pdf")
async def engineering_parts_upload_pdf(
    pdf_file: UploadFile = File(...),
    hk_machine_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    if not pdf_file.filename or not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="PDF file is required.")
    pdf_bytes = await pdf_file.read()
    parsed = parse_hk_cutsheet(pdf_bytes)

    part_id = (parsed.get("primary_part_id") or "").strip()
    if not part_id:
        raise HTTPException(status_code=422, detail="Unable to parse part ID from PDF.")

    parsed_description = (parsed.get("primary_description") or "").strip()

    part = db.query(models.PartMaster).filter_by(part_id=part_id).first()
    if not part:
        part = models.PartMaster(part_id=part_id, description=parsed_description, cur_rev=0)
        db.add(part)
        db.flush()
    elif parsed_description:
        part.description = parsed_description

    selected_rev = max(part.cur_rev, 0)
    existing_header = db.query(models.RevisionHeader).filter_by(part_id=part_id, rev_id=selected_rev).first()
    if not existing_header:
        existing_header = models.RevisionHeader(part_id=part_id, rev_id=selected_rev)
        db.add(existing_header)

    try:
        from io import BytesIO
        from pypdf import PdfReader, PdfWriter
    except Exception as exc:  # pragma: no cover - surfaced to UI
        raise HTTPException(status_code=500, detail="PDF parser dependency is not installed.") from exc

    reader = PdfReader(BytesIO(pdf_bytes))
    if len(reader.pages) < 2:
        raise HTTPException(status_code=422, detail="Uploaded PDF must include at least two pages.")

    hk_pdf_path = PART_FILE_DIR / f"{part_id}_hk.pdf"
    brake_pdf_path = PART_FILE_DIR / f"{part_id}_br.pdf"
    hk_machine_path: Path | None = None
    mpf_filename = ""
    if hk_machine_file and hk_machine_file.filename:
        hk_machine_name = Path(hk_machine_file.filename).name
        hk_machine_path = PART_FILE_DIR / f"{part_id}_{int(datetime.utcnow().timestamp())}_{hk_machine_name}"
        mpf_filename = hk_machine_name

    hk_writer = PdfWriter()
    hk_writer.add_page(reader.pages[0])
    with hk_pdf_path.open("wb") as hk_file:
        hk_writer.write(hk_file)

    brake_writer = PdfWriter()
    brake_writer.add_page(reader.pages[1])
    with brake_pdf_path.open("wb") as brake_file:
        brake_writer.write(brake_file)

    if hk_machine_path:
        hk_machine_bytes = await hk_machine_file.read()
        hk_machine_path.write_bytes(hk_machine_bytes)

    existing_header.hk_file = str(hk_pdf_path)
    existing_header.cut_pdf = str(brake_pdf_path)
    if hk_machine_path:
        existing_header.cut_dwg = str(hk_machine_path)
    existing_header.hk_qty = parsed.get("qty_produced") or 0
    existing_header.released_by = user.username
    existing_header.released_date = datetime.utcnow()

    db.query(models.RevisionBom).filter_by(part_id=part_id, rev_id=selected_rev).delete(synchronize_session=False)
    parsed_components: list[dict] = []
    for component in parsed.get("components", []):
        comp_id = (component.get("component_id") or "").strip()
        assy_qty = component.get("assy_qty")
        if not comp_id or assy_qty in (None, ""):
            continue
        parsed_components.append(component)
        db.add(models.RevisionBom(part_id=part_id, rev_id=selected_rev, comp_id=comp_id, comp_qty=float(assy_qty)))
        ensure_inventory_component_exists(db, comp_id)

    mpf_record = None
    if mpf_filename:
        mpf_record = upsert_mpf_master_with_details(db, mpf_filename=mpf_filename, parsed=parsed, components=parsed_components)

    upsert_engineering_pdf(
        db=db,
        pdf_filename=hk_pdf_path.name,
        pdf_path=hk_pdf_path,
        mpf_master_id=mpf_record.id if mpf_record else None,
        hk_laser=True,
        omax_wj=False,
        bp=False,
        welding=False,
    )
    upsert_engineering_pdf(
        db=db,
        pdf_filename=brake_pdf_path.name,
        pdf_path=brake_pdf_path,
        mpf_master_id=mpf_record.id if mpf_record else None,
        hk_laser=False,
        omax_wj=False,
        bp=True,
        welding=False,
    )

    db.commit()
    return RedirectResponse(f"/engineering/parts/{part_id}?mode=edit&rev_id={selected_rev}", status_code=302)


@app.get("/engineering/wj-gcode", response_class=HTMLResponse)
def engineering_wj_gcode_page(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return templates.TemplateResponse("engineering_machine_program_stub.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "page_title": "WJ Gcode", "page_message": "WJ Gcode dashboard is coming next.", **engineering_nav_context()})


@app.get("/engineering/abb-modules", response_class=HTMLResponse)
def engineering_abb_modules_page(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return templates.TemplateResponse("engineering_machine_program_stub.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "page_title": "ABB Modules", "page_message": "ABB module dashboard is coming next.", **engineering_nav_context()})


@app.get("/engineering/pdfs", response_class=HTMLResponse)
def engineering_pdfs_page(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    rows = db.query(models.EngineeringPdf).order_by(models.EngineeringPdf.created_at.desc()).all()
    mpf_rows = db.query(models.MpfMaster).order_by(models.MpfMaster.mpf_filename.asc()).all()
    return templates.TemplateResponse("engineering_pdfs.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "rows": rows, "mpf_rows": mpf_rows, **engineering_nav_context()})


@app.post("/engineering/pdfs/upload")
async def engineering_pdfs_upload(
    pdf_file: UploadFile = File(...),
    mpf_master_id: int | None = Form(None),
    hk_laser: str | None = Form(None),
    omax_wj: str | None = Form(None),
    bp: str | None = Form(None),
    welding: str | None = Form(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    if not pdf_file.filename or not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="PDF file is required.")
    safe_name = Path(pdf_file.filename).name
    output_path = PDF_DIR / f"{int(datetime.utcnow().timestamp())}_{safe_name}"
    output_path.write_bytes(await pdf_file.read())
    upsert_engineering_pdf(
        db=db,
        pdf_filename=safe_name,
        pdf_path=output_path,
        mpf_master_id=mpf_master_id,
        hk_laser=bool(hk_laser),
        omax_wj=bool(omax_wj),
        bp=bool(bp),
        welding=bool(welding),
    )
    db.commit()
    return RedirectResponse("/engineering/pdfs", status_code=302)


@app.get("/engineering/pdfs/{pdf_id}/view")
def engineering_pdfs_view(pdf_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    row = db.query(models.EngineeringPdf).filter_by(id=pdf_id).first()
    if not row:
        raise HTTPException(404)
    file_path = Path(row.pdf_path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404)
    return FileResponse(path=file_path, filename=row.pdf_filename)


@app.post("/engineering/pdfs/{pdf_id}/edit")
async def engineering_pdfs_edit(pdf_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    row = db.query(models.EngineeringPdf).filter_by(id=pdf_id).first()
    if not row:
        raise HTTPException(404)
    form = await request.form()
    mpf_raw = form.get("mpf_master_id")
    row.mpf_master_id = int(mpf_raw) if mpf_raw else None
    row.hk_laser = bool(form.get("hk_laser"))
    row.omax_wj = bool(form.get("omax_wj"))
    row.bp = bool(form.get("bp"))
    row.welding = bool(form.get("welding"))
    db.commit()
    return RedirectResponse("/engineering/pdfs", status_code=302)


@app.post("/engineering/pdfs/{pdf_id}/delete")
def engineering_pdfs_delete(pdf_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    row = db.query(models.EngineeringPdf).filter_by(id=pdf_id).first()
    if row:
        db.delete(row)
        db.commit()
    return RedirectResponse("/engineering/pdfs", status_code=302)


@app.get("/engineering/drawings", response_class=HTMLResponse)
def engineering_drawings_page(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return templates.TemplateResponse("engineering_machine_program_stub.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "page_title": "Drawings", "page_message": "Drawing dashboard is coming next.", **engineering_nav_context()})


@app.get("/stations", response_class=HTMLResponse)
def stations_dashboard(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    stations = ensure_default_stations(db)
    start_of_day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    station_cards: list[dict] = []
    for station in stations:
        queue_length = db.query(models.Queue).filter(
            models.Queue.station_id == station.id,
            models.Queue.status.in_(["queued", "in_progress"]),
        ).count()
        current_pallet = db.query(models.Pallet).filter_by(current_station_id=station.id, status="in_progress").order_by(models.Pallet.id.desc()).first()
        parts_processed = db.query(func.count(models.PalletEvent.id)).filter(
            models.PalletEvent.station_id == station.id,
            models.PalletEvent.event_type.in_(["completed", "save_work"]),
            models.PalletEvent.recorded_at >= start_of_day,
        ).scalar() or 0
        hours_operated = db.query(func.sum(models.PalletEvent.quantity)).filter(
            models.PalletEvent.station_id == station.id,
            models.PalletEvent.event_type == "hours_operated",
            models.PalletEvent.recorded_at >= start_of_day,
        ).scalar() or 0
        station_cards.append({
            "id": station.id,
            "name": station.station_name,
            "current_pallet": current_pallet.pallet_code if current_pallet else "None",
            "queue_length": queue_length,
            "hours_operated": round(float(hours_operated), 2),
            "parts_processed": int(parts_processed),
        })
    return templates.TemplateResponse("stations_dashboard.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "station_cards": station_cards, **station_nav_context(db)})


@app.get("/stations/{station_id}", response_class=HTMLResponse)
def station_page(station_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    station = db.query(models.Station).filter_by(id=station_id, active=True).first()
    if not station:
        raise HTTPException(404)
    if not request.session.get(f"station_auth_{station_id}"):
        return RedirectResponse(f"/stations/{station_id}/login", status_code=302)

    queue_rows = db.query(models.Queue).filter_by(station_id=station_id).order_by(models.Queue.queue_position.asc()).limit(5).all()
    queue = []
    for q in queue_rows:
        pallet = db.query(models.Pallet).filter_by(id=q.pallet_id).first()
        if not pallet:
            continue
        queue.append({
            "id": q.id,
            "position": q.queue_position,
            "status": q.status,
            "pallet": pallet,
            "components": parse_pallet_component_list(pallet.component_list_json),
        })
    active_pallet = db.query(models.Pallet).filter_by(current_station_id=station_id, status="in_progress").order_by(models.Pallet.id.desc()).first()
    pallet_parts = db.query(models.PalletPart).filter_by(pallet_id=active_pallet.id).all() if active_pallet else []
    station_files = db.query(models.PartRevisionFile).order_by(models.PartRevisionFile.uploaded_at.desc()).all()
    station_documents = [f for f in station_files if str(station_id) in {v.strip() for v in (f.station_ids_csv or "").split(",") if v.strip()}][:10]
    selected_doc_id = request.query_params.get("doc")
    selected_doc = next((f for f in station_documents if str(f.id) == selected_doc_id), station_documents[0] if station_documents else None)

    return templates.TemplateResponse("station_detail.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "station": station, "queue": queue, "active_pallet": active_pallet, "pallet_parts": pallet_parts, "station_documents": station_documents, "selected_doc": selected_doc, **station_nav_context(db)})


@app.get("/stations/login", response_class=HTMLResponse)
def stations_login(db: Session = Depends(get_db), user=Depends(require_login)):
    station = ensure_default_stations(db)[0]
    return RedirectResponse(f"/stations/{station.id}/login", status_code=302)


@app.get("/stations/{station_id}/login", response_class=HTMLResponse)
def station_login(station_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    station = db.query(models.Station).filter_by(id=station_id, active=True).first()
    if not station:
        raise HTTPException(404)
    return templates.TemplateResponse("station_login.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "station": station, "error": None, "ok": None, **station_nav_context(db)})


@app.post("/stations/{station_id}/login", response_class=HTMLResponse)
def stations_login_submit(station_id: int, request: Request, station_user_id: str = Form(...), station_password: str = Form(...), db: Session = Depends(get_db), user=Depends(require_login)):
    station = db.query(models.Station).filter_by(id=station_id, active=True).first()
    if not station:
        raise HTTPException(404)
    account = db.query(models.Employee).filter_by(username=station_user_id.strip(), active=True).first()
    if account and verify_password(station_password, account.password_hash):
        request.session[f"station_auth_{station_id}"] = station_user_id.strip()
        return RedirectResponse(f"/stations/{station_id}", status_code=302)
    return templates.TemplateResponse("station_login.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "station": station, "error": "Invalid station credentials", "ok": None, **station_nav_context(db)})


@app.post("/stations/{station_id}/start-next")
def station_start_next(station_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    next_queue = db.query(models.Queue).filter_by(station_id=station_id, status="queued").order_by(models.Queue.queue_position.asc()).first()
    if next_queue:
        next_queue.status = "in_progress"
        pallet = db.query(models.Pallet).filter_by(id=next_queue.pallet_id).first()
        if pallet:
            pallet.status = "in_progress"
            pallet.current_station_id = station_id
            route_row = db.query(models.PalletStationRoute).filter_by(pallet_id=pallet.id, station_id=station_id).first()
            if route_row:
                route_row.status = "in-process"
                route_row.location_id = f"S{station_id}"
            db.add(models.PalletEvent(pallet_id=pallet.id, station_id=station_id, event_type="started", quantity=0, recorded_by=user.username, notes="Started next queued pallet"))
        db.commit()
    return RedirectResponse(f"/stations/{station_id}", status_code=302)


@app.get("/stations/{station_id}/complete", response_class=HTMLResponse)
def station_complete_pallet_form(station_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    station = db.query(models.Station).filter_by(id=station_id, active=True).first()
    if not station:
        raise HTTPException(404)

    pallet = db.query(models.Pallet).filter_by(current_station_id=station_id, status="in_progress").order_by(models.Pallet.id.desc()).first()
    if not pallet:
        return RedirectResponse(f"/stations/{station_id}", status_code=302)

    part_rows = get_pallet_part_rows(db, pallet)
    available_bins = get_available_pallet_bins(db)
    return templates.TemplateResponse("station_complete_pallet.html", {
        "request": request,
        "user": user,
        "top_nav": TOP_NAV,
        "entity_groups": ENTITY_GROUPS,
        "station": station,
        "pallet": pallet,
        "part_rows": part_rows,
        "available_bins": available_bins,
        **station_nav_context(db),
    })


@app.post("/stations/{station_id}/complete")
async def station_complete_pallet_submit(station_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    station = db.query(models.Station).filter_by(id=station_id, active=True).first()
    if not station:
        raise HTTPException(404)

    pallet = db.query(models.Pallet).filter_by(current_station_id=station_id, status="in_progress").order_by(models.Pallet.id.desc()).first()
    if not pallet:
        return RedirectResponse(f"/stations/{station_id}", status_code=302)

    form = await request.form()
    component_ids = form.getlist("component_id")
    qty_expected_list = form.getlist("qty_expected")
    qty_completed_list = form.getlist("qty_completed")
    qty_scrap_list = form.getlist("qty_scrap")

    total_completed = 0.0
    for idx, component_id_raw in enumerate(component_ids):
        component_id = (component_id_raw or "").strip()
        if not component_id:
            continue
        qty_expected = float(qty_expected_list[idx] or 0) if idx < len(qty_expected_list) else 0
        qty_completed = float(qty_completed_list[idx] or 0) if idx < len(qty_completed_list) else 0
        qty_scrap = float(qty_scrap_list[idx] or 0) if idx < len(qty_scrap_list) else 0

        db.add(models.PalletComponentStationLog(
            pallet_id=pallet.id,
            station_id=station_id,
            component_id=component_id,
            qty_expected=qty_expected,
            qty_completed=qty_completed,
            qty_scrap=qty_scrap,
            recorded_by=user.username,
        ))

        if component_id == (pallet.frame_part_number or ""):
            total_completed = qty_completed

        revision = db.query(models.PartRevision).join(models.Part, models.Part.id == models.PartRevision.part_id).filter(
            models.Part.part_number == component_id
        ).order_by(models.PartRevision.is_current.desc(), models.PartRevision.id.desc()).first()
        if revision:
            pallet_part = db.query(models.PalletPart).filter_by(pallet_id=pallet.id, part_revision_id=revision.id).first()
            if pallet_part:
                if station_id == 1:
                    pallet_part.actual_quantity = float(pallet_part.actual_quantity or 0) + qty_completed
                else:
                    pallet_part.actual_quantity = qty_completed
                pallet_part.scrap_quantity = qty_scrap

    route_row = db.query(models.PalletStationRoute).filter_by(pallet_id=pallet.id, station_id=station_id).first()
    if route_row:
        route_row.qty_completed = total_completed
        route_row.qty_scrap = sum(float(qty_scrap_list[i] or 0) if i < len(qty_scrap_list) else 0 for i in range(len(component_ids)))
        route_row.status = "complete"
        route_row.location_id = f"S{station_id}"

    active_queue = db.query(models.Queue).filter_by(station_id=station_id, pallet_id=pallet.id, status="in_progress").first()
    if active_queue:
        active_queue.status = "done"

    expected_total = float(pallet.expected_quantity or 0)
    spawn_leftover = (form.get("spawn_leftover") or "") == "yes"
    if spawn_leftover and expected_total > total_completed:
        leftover = expected_total - total_completed
        new_pallet = models.Pallet(
            pallet_code=f"{pallet.pallet_code}-SPLIT-{int(datetime.utcnow().timestamp())}",
            pallet_type="split",
            production_order_id=pallet.production_order_id,
            mpf_master_id=pallet.mpf_master_id,
            frame_part_number=pallet.frame_part_number,
            expected_quantity=leftover,
            sheet_count=pallet.sheet_count,
            component_list_json=pallet.component_list_json,
            status="staged",
            created_by=user.username,
            parent_pallet_id=pallet.id,
        )
        db.add(new_pallet)
        db.flush()
        original_parts = db.query(models.PalletPart).filter_by(pallet_id=pallet.id).all()
        for part in original_parts:
            ratio = (leftover / expected_total) if expected_total > 0 else 0
            db.add(models.PalletPart(
                pallet_id=new_pallet.id,
                part_revision_id=part.part_revision_id,
                planned_quantity=(part.planned_quantity or 0) * ratio,
                external_quantity_needed=(part.external_quantity_needed or 0) * ratio,
                actual_quantity=0,
                scrap_quantity=0,
            ))
        ensure_pallet_station_routing(db, new_pallet)

    route_choice = (form.get("route_choice") or "queue_next").strip()
    next_route_row = get_next_route_row(db, pallet.id, station_id)
    if route_choice == "store":
        storage_bin_id = form.get("storage_bin_id")
        if storage_bin_id:
            storage_bin = db.query(models.StorageBin).filter_by(id=int(storage_bin_id)).first()
            if storage_bin:
                assign_pallet_to_storage_bin(db, pallet, storage_bin)
        pallet.status = "staged"
    elif next_route_row and next_route_row.station_id:
        queue_pallet_for_station(db, pallet, next_route_row.station_id)
        next_route_row.status = "queued"
        next_route_row.location_id = f"Q{next_route_row.station_id}"
        pallet.status = "queued"
        pallet.current_station_id = None
        pallet.storage_bin_id = None
    else:
        pallet.status = "complete"
        pallet.current_station_id = None

    db.add(models.PalletEvent(
        pallet_id=pallet.id,
        station_id=station_id,
        event_type="completed",
        quantity=total_completed,
        recorded_by=user.username,
        notes="Completed pallet at station",
    ))
    db.commit()
    return RedirectResponse(f"/stations/{station_id}", status_code=302)


@app.post("/stations/{station_id}/save-work")
def station_save_work(station_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = db.query(models.Pallet).filter_by(current_station_id=station_id, status="in_progress").order_by(models.Pallet.id.desc()).first()
    if pallet:
        db.add(models.PalletEvent(pallet_id=pallet.id, station_id=station_id, event_type="save_work", quantity=0, recorded_by=user.username, notes="Saved pallet work"))
        db.commit()
    return RedirectResponse(f"/stations/{station_id}", status_code=302)


@app.post("/stations/{station_id}/queue-reorder")
async def station_queue_reorder(station_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    payload = await request.json()
    for idx, queue_id in enumerate(payload.get("order", []), start=1):
        row = db.query(models.Queue).filter_by(id=int(queue_id), station_id=station_id).first()
        if row:
            row.queue_position = idx
    db.commit()
    return {"ok": True}


@app.post("/stations/report-engineering-issue")
def stations_report_engineering_issue(station_id: int = Form(...), pallet_id: int | None = Form(None), question_text: str = Form(...), db: Session = Depends(get_db), user=Depends(require_login)):
    db.add(models.EngineeringQuestion(station_id=station_id, pallet_id=pallet_id, asked_by=user.username, question_text=question_text, status="open"))
    db.commit()
    return RedirectResponse("/stations", status_code=302)


@app.get("/maintenance", response_class=HTMLResponse)
def maintenance_dashboard(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    ensure_upcoming_scheduled_requests(db)
    open_requests = db.query(models.MaintenanceRequest).filter(
        models.MaintenanceRequest.request_type == "request",
        models.MaintenanceRequest.status != "complete",
    ).order_by(models.MaintenanceRequest.created_at.desc()).all()
    upcoming = db.query(models.MaintenanceRequest).filter(
        models.MaintenanceRequest.request_type == "scheduled",
        models.MaintenanceRequest.status != "complete",
        models.MaintenanceRequest.scheduled_for <= (datetime.utcnow() + timedelta(days=14)),
    ).order_by(models.MaintenanceRequest.scheduled_for.asc(), models.MaintenanceRequest.created_at.asc()).all()
    stations = db.query(models.Station).order_by(models.Station.station_name.asc()).all()
    return templates.TemplateResponse("maintenance_dashboard.html", {
        "request": request,
        "user": user,
        "top_nav": TOP_NAV,
        "entity_groups": ENTITY_GROUPS,
        "open_requests": open_requests,
        "upcoming": upcoming,
        "stations": stations,
        **maintenance_station_nav_context(db),
    })


@app.get("/maintenance/stations/{station_id}/edit", response_class=HTMLResponse)
def maintenance_station_edit(station_id: int, request: Request, tab: str = "maintenance", db: Session = Depends(get_db), user=Depends(require_login)):
    station = db.query(models.Station).filter_by(id=station_id).first()
    if not station:
        raise HTTPException(404)
    stations = db.query(models.Station).order_by(models.Station.station_name.asc()).all()
    skills = db.query(models.Skill).order_by(models.Skill.name.asc()).all()
    tasks = db.query(models.StationMaintenanceTask).filter_by(station_id=station_id).order_by(models.StationMaintenanceTask.id.desc()).all()
    logs = db.query(models.MaintenanceLog).filter_by(station_id=station_id).order_by(models.MaintenanceLog.closed_at.desc()).all()
    consumables = db.query(models.Consumable).filter_by(station_id=station_id).order_by(models.Consumable.description.asc()).all()
    return templates.TemplateResponse("maintenance_station_edit.html", {
        "request": request,
        "user": user,
        "top_nav": TOP_NAV,
        "entity_groups": ENTITY_GROUPS,
        "station": station,
        "stations": stations,
        "skills": skills,
        "status_choices": FIELD_CHOICES[("stations", "station_status")],
        "tasks": tasks,
        "logs": logs,
        "consumables": consumables,
        "active_tab": tab if tab in {"maintenance", "consumables", "log"} else "maintenance",
        **maintenance_station_nav_context(db),
    })


@app.post("/maintenance/stations/{station_id}/title")
async def maintenance_station_save_title(station_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    station = db.query(models.Station).filter_by(id=station_id).first()
    if not station:
        raise HTTPException(404)
    form = await request.form()
    station_code = (form.get("station_code") or "").strip()
    station_name = (form.get("station_name") or "").strip()
    if not station_code.isdigit() or len(station_code) != 2:
        raise HTTPException(422, "Station ID must be exactly 2 digits")
    if not station_name:
        raise HTTPException(422, "Station name is required")
    station.station_code = station_code
    station.station_name = station_name
    db.commit()
    return RedirectResponse(f"/maintenance/stations/{station_id}/edit", status_code=302)


@app.post("/maintenance/stations/{station_id}/settings")
async def maintenance_station_save_settings(station_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    station = db.query(models.Station).filter_by(id=station_id).first()
    if not station:
        raise HTTPException(404)
    form = await request.form()
    skill_required = (form.get("skill_required") or "").strip()
    station_status = (form.get("station_status") or "ready/idle").strip()
    if station_status not in FIELD_CHOICES[("stations", "station_status")]:
        raise HTTPException(422, "Invalid station status")
    station.skill_required = skill_required
    station.station_status = station_status
    db.commit()
    return RedirectResponse(f"/maintenance/stations/{station_id}/edit?tab={(form.get('tab') or 'maintenance')}", status_code=302)


@app.post("/maintenance/stations/{station_id}/tasks/new")
def maintenance_station_add_task(station_id: int, task_description: str = Form(...), frequency_hours: float = Form(...), responsible_role: str = Form("maintenance"), db: Session = Depends(get_db), user=Depends(require_login)):
    station = db.query(models.Station).filter_by(id=station_id).first()
    if not station:
        raise HTTPException(404)
    db.add(models.StationMaintenanceTask(
        station_id=station_id,
        task_description=task_description,
        frequency_hours=max(frequency_hours, 1),
        responsible_role=responsible_role or "maintenance",
        active=True,
        next_due_at=datetime.utcnow() + timedelta(hours=max(frequency_hours, 1)),
    ))
    db.commit()
    return RedirectResponse(f"/maintenance/stations/{station_id}/edit", status_code=302)


@app.post("/maintenance/stations/{station_id}/tasks/{task_id}/save")
def maintenance_station_save_task(station_id: int, task_id: int, task_description: str = Form(...), frequency_hours: float = Form(...), responsible_role: str = Form("maintenance"), active: str | None = Form(None), db: Session = Depends(get_db), user=Depends(require_login)):
    task = db.query(models.StationMaintenanceTask).filter_by(id=task_id, station_id=station_id).first()
    if not task:
        raise HTTPException(404)
    task.task_description = task_description
    task.frequency_hours = max(frequency_hours, 1)
    task.responsible_role = responsible_role or "maintenance"
    task.active = active == "on"
    db.commit()
    return RedirectResponse(f"/maintenance/stations/{station_id}/edit", status_code=302)


@app.post("/maintenance/stations/{station_id}/tasks/{task_id}/delete")
def maintenance_station_delete_task(station_id: int, task_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    task = db.query(models.StationMaintenanceTask).filter_by(id=task_id, station_id=station_id).first()
    if task:
        db.delete(task)
        db.commit()
    return RedirectResponse(f"/maintenance/stations/{station_id}/edit", status_code=302)


@app.post("/maintenance/stations/{station_id}/log/new")
def maintenance_station_add_log(station_id: int, closure_notes: str = Form(""), db: Session = Depends(get_db), user=Depends(require_login)):
    station = db.query(models.Station).filter_by(id=station_id).first()
    if not station:
        raise HTTPException(404)
    req = models.MaintenanceRequest(
        station_id=station_id,
        requested_by=user.username,
        requested_user_id=user.id,
        priority="normal",
        status="complete",
        issue_description=closure_notes or "Manual maintenance log entry",
        work_comments=closure_notes,
        request_type="request",
        completed_at=datetime.utcnow(),
    )
    db.add(req)
    db.flush()
    db.add(models.MaintenanceLog(
        maintenance_request_id=req.id,
        station_id=station_id,
        closed_by=user.username,
        closure_notes=closure_notes,
        closed_at=datetime.utcnow(),
    ))
    db.commit()
    return RedirectResponse(f"/maintenance/stations/{station_id}/edit", status_code=302)


@app.get("/maintenance/{request_id}", response_class=HTMLResponse)
def maintenance_request_detail(request_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    maint = db.query(models.MaintenanceRequest).filter_by(id=request_id).first()
    if not maint:
        raise HTTPException(404)
    usage_logs = db.query(models.ConsumableUsageLog).filter(models.ConsumableUsageLog.reason.like(f"maintenance_request:{request_id}:%")).order_by(models.ConsumableUsageLog.logged_at.asc()).all()
    consumables = db.query(models.Consumable).order_by(models.Consumable.description.asc()).all()
    return templates.TemplateResponse("maintenance_request_detail.html", {
        "request": request,
        "user": user,
        "top_nav": TOP_NAV,
        "entity_groups": ENTITY_GROUPS,
        "maint": maint,
        "usage_logs": usage_logs,
        "consumables": consumables,
        "status_choices": FIELD_CHOICES[("maintenance_requests", "status")],
        **maintenance_station_nav_context(db),
    })


@app.post("/maintenance/{request_id}/consumables")
def maintenance_add_consumable(request_id: int, consumable_id: int = Form(...), quantity_used: float = Form(...), db: Session = Depends(get_db), user=Depends(require_login)):
    maint = db.query(models.MaintenanceRequest).filter_by(id=request_id).first()
    if not maint:
        raise HTTPException(404)
    if maint.status == "complete":
        return RedirectResponse(f"/maintenance/{request_id}", status_code=302)
    db.add(models.ConsumableUsageLog(
        consumable_id=consumable_id,
        station_id=maint.station_id,
        quantity_delta=-abs(quantity_used),
        reason=f"maintenance_request:{request_id}:{user.username}",
    ))
    db.commit()
    return RedirectResponse(f"/maintenance/{request_id}", status_code=302)


@app.post("/maintenance/{request_id}/save")
def maintenance_save(request_id: int, work_comments: str = Form(""), status: str = Form("submitted"), db: Session = Depends(get_db), user=Depends(require_login)):
    maint = db.query(models.MaintenanceRequest).filter_by(id=request_id).first()
    if not maint:
        raise HTTPException(404)
    if maint.status == "complete":
        return RedirectResponse(f"/maintenance/{request_id}", status_code=302)
    if status not in FIELD_CHOICES[("maintenance_requests", "status")]:
        raise HTTPException(422)

    maint.work_comments = work_comments
    maint.status = status
    if status == "complete":
        maint.completed_at = datetime.utcnow()
        db.add(models.MaintenanceLog(
            maintenance_request_id=maint.id,
            station_id=maint.station_id,
            closed_by=user.username,
            closure_notes=work_comments,
            closed_at=maint.completed_at,
        ))
        if maint.maintenance_task_id:
            task = db.query(models.StationMaintenanceTask).filter_by(id=maint.maintenance_task_id).first()
            if task:
                task.last_completed_at = maint.completed_at
                task.next_due_at = maint.completed_at + timedelta(hours=task.frequency_hours)
    db.commit()
    return RedirectResponse("/maintenance" if status == "complete" else f"/maintenance/{request_id}", status_code=302)




def ensure_storage_bins(db: Session, location: models.StorageLocation):
    existing = {(b.shelf_id, b.bin_id) for b in db.query(models.StorageBin).filter_by(storage_location_id=location.id).all()}
    for shelf_id in range(1, max(location.shelf_count, 0) + 1):
        for bin_id in range(1, max(location.bin_count, 0) + 1):
            if (shelf_id, bin_id) not in existing:
                holder_id = f"{location.id}.{bin_id}" if location.shelf_count <= 1 else f"{location.id}.{shelf_id}.{bin_id}"
                db.add(models.StorageBin(
                    storage_location_id=location.id,
                    shelf_id=shelf_id,
                    bin_id=bin_id,
                    location_id=holder_id,
                    description="location holder",
                ))
    db.commit()


@app.get("/inventory", response_class=HTMLResponse)
def inventory_dashboard(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    consumables = db.query(models.Consumable).order_by(models.Consumable.description.asc()).all()
    raw_materials = db.query(models.RawMaterial).order_by(models.RawMaterial.id.asc()).all()

    low_stock_rows = []
    for consumable in consumables:
        if consumable.qty_on_hand <= consumable.reorder_point:
            low_stock_rows.append({
                "item_type": "Consumable",
                "id": consumable.id,
                "description": consumable.description,
                "qty_on_hand": consumable.qty_on_hand,
                "reorder_qty": consumable.reorder_point,
                "qty_on_order": consumable.qty_on_order,
                "qty_on_request": consumable.qty_on_request,
            })
    for material in raw_materials:
        if material.qty_on_request > 0 and material.qty_on_hand <= material.qty_on_request:
            low_stock_rows.append({
                "item_type": "Raw Material",
                "id": material.id,
                "description": f"Gauge {material.gauge} ({material.length} x {material.width})",
                "qty_on_hand": material.qty_on_hand,
                "reorder_qty": material.qty_on_request,
                "qty_on_order": material.qty_on_order,
                "qty_on_request": material.qty_on_request,
            })

    open_purchase_requests = (
        db.query(
            models.PurchaseRequest.id,
            models.PurchaseRequest.requested_at,
            models.PurchaseRequest.requested_by,
            models.PurchaseRequest.status,
            func.count(models.PurchaseRequestLine.id).label("line_count"),
            func.coalesce(func.sum(models.PurchaseRequestLine.quantity), 0).label("total_requested_qty"),
        )
        .outerjoin(
            models.PurchaseRequestLine,
            models.PurchaseRequestLine.purchase_request_id == models.PurchaseRequest.id,
        )
        .filter(models.PurchaseRequest.status == "open")
        .group_by(
            models.PurchaseRequest.id,
            models.PurchaseRequest.requested_at,
            models.PurchaseRequest.requested_by,
            models.PurchaseRequest.status,
        )
        .order_by(models.PurchaseRequest.requested_at.desc())
        .all()
    )

    on_order_rows = []
    for consumable in consumables:
        if consumable.qty_on_order > 0:
            on_order_rows.append({
                "item_type": "Consumable",
                "id": consumable.id,
                "description": consumable.description,
                "qty_on_hand": consumable.qty_on_hand,
                "qty_on_order": consumable.qty_on_order,
                "qty_on_request": consumable.qty_on_request,
            })
    for material in raw_materials:
        if material.qty_on_order > 0:
            on_order_rows.append({
                "item_type": "Raw Material",
                "id": material.id,
                "description": f"Gauge {material.gauge} ({material.length} x {material.width})",
                "qty_on_hand": material.qty_on_hand,
                "qty_on_order": material.qty_on_order,
                "qty_on_request": material.qty_on_request,
            })

    return templates.TemplateResponse(
        "inventory_dashboard.html",
        {
            "request": request,
            "user": user,
            "top_nav": TOP_NAV,
            "entity_groups": ENTITY_GROUPS,
            "low_stock_rows": low_stock_rows,
            "open_purchase_requests": open_purchase_requests,
            "on_order_rows": on_order_rows,
        },
    )


@app.get("/inventory/locations", response_class=HTMLResponse)
def storage_location_list(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    rows = db.query(models.StorageLocation).order_by(models.StorageLocation.id.asc()).all()
    used_bins_by_location = {
        location_id: used_bins
        for location_id, used_bins in (
            db.query(
                models.StorageBin.storage_location_id,
                func.count(models.StorageBin.id),
            )
            .filter(
                func.trim(func.coalesce(models.StorageBin.component_id, "")) != ""
            )
            .group_by(models.StorageBin.storage_location_id)
            .all()
        )
    }
    return templates.TemplateResponse(
        "storage_locations.html",
        {
            "request": request,
            "user": user,
            "top_nav": TOP_NAV,
            "entity_groups": ENTITY_GROUPS,
            "rows": rows,
            "used_bins_by_location": used_bins_by_location,
        },
    )


@app.post("/inventory/locations/add")
async def storage_location_add(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    form = await request.form()
    location = models.StorageLocation(
        location_description=(form.get("location_description") or "").strip(),
        pallet_storage=(form.get("pallet_storage") == "on"),
        shelf_count=int(form.get("shelf_count") or 1),
        bin_count=int(form.get("bin_count") or 1),
    )
    db.add(location)
    db.commit()
    db.refresh(location)
    ensure_storage_bins(db, location)
    return RedirectResponse("/inventory/locations", status_code=303)


@app.get("/inventory/locations/{location_id}", response_class=HTMLResponse)
def storage_location_detail(location_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    location = db.query(models.StorageLocation).filter_by(id=location_id).first()
    if not location:
        raise HTTPException(404)
    ensure_storage_bins(db, location)
    bins = db.query(models.StorageBin).filter_by(storage_location_id=location_id).order_by(models.StorageBin.shelf_id.asc(), models.StorageBin.bin_id.asc()).all()
    shelves = {}
    for b in bins:
        shelves.setdefault(b.shelf_id, []).append(b)
    return templates.TemplateResponse("storage_location_detail.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "location": location, "shelves": shelves})


@app.get("/inventory/locations/{location_id}/edit", response_class=HTMLResponse)
def storage_location_edit_form(location_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    location = db.query(models.StorageLocation).filter_by(id=location_id).first()
    if not location:
        raise HTTPException(404)
    return templates.TemplateResponse("storage_location_edit.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "location": location})


@app.post("/inventory/locations/{location_id}/edit")
async def storage_location_edit(location_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    location = db.query(models.StorageLocation).filter_by(id=location_id).first()
    if not location:
        raise HTTPException(404)
    form = await request.form()
    location.location_description = (form.get("location_description") or "").strip()
    location.pallet_storage = (form.get("pallet_storage") == "on")
    location.shelf_count = int(form.get("shelf_count") or 1)
    location.bin_count = int(form.get("bin_count") or 1)
    db.commit()
    ensure_storage_bins(db, location)
    return RedirectResponse("/inventory/locations", status_code=303)


@app.post("/inventory/locations/{location_id}/delete")
def storage_location_delete(location_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    location = db.query(models.StorageLocation).filter_by(id=location_id).first()
    if location:
        db.query(models.StorageBin).filter_by(storage_location_id=location_id).delete()
        db.delete(location)
        db.commit()
    return RedirectResponse("/inventory/locations", status_code=302)


@app.post("/inventory/storage-bins/{bin_id}/edit")
async def storage_bin_edit(bin_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    row = db.query(models.StorageBin).filter_by(id=bin_id).first()
    if not row:
        raise HTTPException(404)
    form = await request.form()
    row.qty = float(form.get("qty") or 0)
    row.location_id = (form.get("location_id") or "").strip()
    row.component_id = (form.get("component_id") or "").strip()
    row.description = (form.get("description") or "").strip()
    db.commit()
    return RedirectResponse(f"/inventory/locations/{row.storage_location_id}", status_code=302)


@app.get("/inventory/raw-materials", response_class=HTMLResponse)
def raw_materials_page(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    rows = db.query(models.RawMaterial).order_by(models.RawMaterial.id.asc()).all()
    locations = db.query(models.StorageLocation).order_by(models.StorageLocation.id.asc()).all()
    return templates.TemplateResponse("raw_materials.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "rows": rows, "locations": locations})


@app.post("/inventory/raw-materials/add")
async def raw_materials_add(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    form = await request.form()
    row = models.RawMaterial(
        gauge=(form.get("gauge") or "").strip(),
        length=float(form.get("length") or 0),
        width=float(form.get("width") or 0),
        qty_on_hand=float(form.get("qty_on_hand") or 0),
        qty_on_request=float(form.get("qty_on_request") or 0),
        qty_on_order=float(form.get("qty_on_order") or 0),
        storage_location_id=int(form.get("storage_location_id")) if form.get("storage_location_id") else None,
    )
    db.add(row)
    db.commit()
    return RedirectResponse("/inventory/raw-materials", status_code=302)


@app.post("/inventory/raw-materials/{material_id}/edit")
async def raw_materials_edit(material_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    row = db.query(models.RawMaterial).filter_by(id=material_id).first()
    if not row:
        raise HTTPException(404)
    form = await request.form()
    row.gauge = (form.get("gauge") or "").strip()
    row.length = float(form.get("length") or 0)
    row.width = float(form.get("width") or 0)
    row.qty_on_hand = float(form.get("qty_on_hand") or 0)
    row.qty_on_request = float(form.get("qty_on_request") or 0)
    row.qty_on_order = float(form.get("qty_on_order") or 0)
    row.storage_location_id = int(form.get("storage_location_id")) if form.get("storage_location_id") else None
    db.commit()
    return RedirectResponse("/inventory/raw-materials", status_code=302)


@app.post("/inventory/raw-materials/{material_id}/delete")
def raw_materials_delete(material_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    row = db.query(models.RawMaterial).filter_by(id=material_id).first()
    if row:
        db.delete(row)
        db.commit()
    return RedirectResponse("/inventory/raw-materials", status_code=302)


@app.get("/inventory/consumables", response_class=HTMLResponse)
def consumables_page(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    stations = db.query(models.Station).order_by(models.Station.station_name.asc()).all()
    locations = db.query(models.StorageLocation).order_by(models.StorageLocation.id.asc()).all()
    rows = db.query(models.Consumable).order_by(models.Consumable.id.asc()).all()
    grouped = {s.id: [] for s in stations}
    for row in rows:
        grouped.setdefault(row.station_id or 0, []).append(row)
    return templates.TemplateResponse(
        "consumables_inventory.html",
        {
            "request": request,
            "user": user,
            "top_nav": TOP_NAV,
            "entity_groups": ENTITY_GROUPS,
            "stations": stations,
            "locations": locations,
            "grouped": grouped,
        },
    )


@app.get("/inventory/consumables/{consumable_id}", response_class=HTMLResponse)
def consumable_detail(consumable_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    consumable = db.query(models.Consumable).filter_by(id=consumable_id).first()
    if not consumable:
        raise HTTPException(404)
    stations = db.query(models.Station).order_by(models.Station.station_name.asc()).all()
    locations = db.query(models.StorageLocation).order_by(models.StorageLocation.id.asc()).all()
    return templates.TemplateResponse(
        "consumable_detail.html",
        {
            "request": request,
            "user": user,
            "top_nav": TOP_NAV,
            "entity_groups": ENTITY_GROUPS,
            "consumable": consumable,
            "stations": stations,
            "locations": locations,
        },
    )


@app.post("/inventory/consumables/{consumable_id}/edit")
async def consumable_edit(consumable_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    consumable = db.query(models.Consumable).filter_by(id=consumable_id).first()
    if not consumable:
        raise HTTPException(404)
    form = await request.form()
    consumable.description = (form.get("description") or "").strip()
    consumable.vendor = (form.get("vendor") or "").strip()
    consumable.vendor_part_number = (form.get("vendor_part_number") or "").strip()
    consumable.unit_cost = float(form.get("unit_cost") or 0)
    consumable.qty_on_hand = float(form.get("qty_on_hand") or 0)
    consumable.qty_on_order = float(form.get("qty_on_order") or 0)
    consumable.qty_on_request = float(form.get("qty_on_request") or 0)
    consumable.reorder_point = float(form.get("reorder_point") or 0)
    consumable.station_id = int(form.get("station_id")) if form.get("station_id") else None
    consumable.location_id = int(form.get("location_id")) if form.get("location_id") else None
    db.commit()
    return RedirectResponse(f"/inventory/consumables/{consumable.id}", status_code=302)


@app.get("/inventory/scrap-steel", response_class=HTMLResponse)
def scrap_steel_page(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    rows = db.query(models.ScrapSteel).order_by(models.ScrapSteel.id.asc()).all()
    locations = db.query(models.StorageLocation).order_by(models.StorageLocation.id.asc()).all()
    return templates.TemplateResponse("scrap_steel.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "rows": rows, "locations": locations})




@app.post("/inventory/scrap-steel/add")
async def scrap_steel_add(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    form = await request.form()
    row = models.ScrapSteel(
        pallet_id=(form.get("pallet_id") or "").strip(),
        storage_id=(form.get("storage_id") or "").strip(),
        weight=float(form.get("weight") or 0),
        location_id=int(form.get("location_id")) if form.get("location_id") else None,
        scrap_type=(form.get("scrap_type") or "").strip(),
    )
    db.add(row)
    db.commit()
    return RedirectResponse("/inventory/scrap-steel", status_code=302)

@app.post("/inventory/scrap-steel/{scrap_id}/edit")
async def scrap_steel_edit(scrap_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    row = db.query(models.ScrapSteel).filter_by(id=scrap_id).first()
    if not row:
        raise HTTPException(404)
    form = await request.form()
    row.pallet_id = (form.get("pallet_id") or "").strip()
    row.storage_id = (form.get("storage_id") or "").strip()
    row.weight = float(form.get("weight") or 0)
    row.location_id = int(form.get("location_id")) if form.get("location_id") else None
    row.scrap_type = (form.get("scrap_type") or "").strip()
    db.commit()
    return RedirectResponse("/inventory/scrap-steel", status_code=302)


@app.post("/inventory/scrap-steel/{scrap_id}/deliver")
def scrap_steel_deliver(scrap_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    row = db.query(models.ScrapSteel).filter_by(id=scrap_id).first()
    if row:
        row.delivered = True
        db.commit()
    return RedirectResponse("/inventory/scrap-steel", status_code=302)


@app.get("/inventory/parts", response_class=HTMLResponse)
def parts_inventory_page(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    rows = db.query(models.Part, models.PartInventory).outerjoin(models.PartInventory, models.PartInventory.part_id == models.Part.id).order_by(models.Part.part_number.asc()).all()
    return templates.TemplateResponse("parts_inventory.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "rows": rows})


@app.post("/inventory/parts/{part_id}/edit")
async def part_inventory_edit(part_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    part = db.query(models.Part).filter_by(id=part_id).first()
    if not part:
        raise HTTPException(404)

    form = await request.form()
    inventory = db.query(models.PartInventory).filter_by(part_id=part_id).first()
    if not inventory:
        inventory = models.PartInventory(part_id=part_id)
        db.add(inventory)
        db.flush()

    inventory.qty_on_hand_total = float(form.get("qty_on_hand_total") or 0)
    inventory.qty_stored = float(form.get("qty_stored") or 0)
    inventory.qty_queued_to_cut = float(form.get("qty_queued_to_cut") or 0)
    inventory.qty_to_bend = float(form.get("qty_to_bend") or 0)
    inventory.qty_to_weld = float(form.get("qty_to_weld") or 0)
    db.commit()
    return RedirectResponse("/inventory/parts", status_code=302)


@app.get("/inventory/delivered-parts", response_class=HTMLResponse)
def delivered_parts_page(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    rows = db.query(models.DeliveredPartLot).order_by(models.DeliveredPartLot.completed_at.desc()).all()
    return templates.TemplateResponse("delivered_parts.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "rows": rows})

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(models.Employee).filter_by(username=username, active=True).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})
    request.session["uid"] = user.id
    return RedirectResponse("/", status_code=302)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/entity/{entity}", response_class=HTMLResponse)
def entity_list(entity: str, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    model = MODEL_MAP.get(entity)
    if not model:
        raise HTTPException(404)
    rows = db.query(model).limit(200).all()
    cols = [c.name for c in model.__table__.columns]
    return templates.TemplateResponse("entity_list.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "entity": entity, "rows": rows, "cols": cols, "can_write": can_write(user, entity)})


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, tab: str = "employees", db: Session = Depends(get_db), user=Depends(require_admin)):
    tab = tab if tab in {"stations", "skills", "employees", "server-maintenance"} else "employees"
    admin_tab_titles = {
        "stations": "Stations",
        "skills": "Skills",
        "employees": "Employees",
        "server-maintenance": "Server Maintenance",
    }
    tab_data = {
        "stations": db.query(models.Station).order_by(models.Station.id.desc()).limit(200).all(),
        "skills": db.query(models.Skill).order_by(models.Skill.id.desc()).limit(200).all(),
        "employees": db.query(models.Employee).order_by(models.Employee.id.desc()).limit(200).all(),
    }

    branches, active_branch = list_branches()

    admin_cols = {k: [c.name for c in MODEL_MAP[k].__table__.columns] for k in ["stations", "skills", "employees"]}

    return templates.TemplateResponse("admin_dashboard.html", {
        "request": request,
        "user": user,
        "top_nav": TOP_NAV,
        "entity_groups": ENTITY_GROUPS,
        "active_tab": tab,
        "active_tab_title": admin_tab_titles[tab],
        "tab_data": tab_data,
        "admin_cols": admin_cols,
        "branches": branches,
        "active_branch": active_branch,
        "data_paths": {
            "DRAWING_DATA_PATH": str(DRAWING_DIR),
            "PDF_DATA_PATH": str(PDF_DIR),
            "PART_FILE_DATA_PATH": str(PART_FILE_DIR),
            "SQL_DATA_PATH": RUNTIME_SETTINGS.get("SQL_DATA_PATH") or os.getenv("SQL_DATA_PATH", "/data/sql/mts.db"),
            "settings_path": str(SETTINGS_PATH),
        },
        "message": request.query_params.get("message"),
    })


@app.get("/admin/{entity}/{item_id}/view", response_class=HTMLResponse)
def admin_entity_view(entity: str, item_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_admin)):
    if entity not in {"employees", "stations", "skills"}:
        raise HTTPException(404)
    model = MODEL_MAP.get(entity)
    item = db.query(model).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(404)
    cols = [c for c in model.__table__.columns if c.name != "id"]
    field_meta = {c.name: build_field_meta(entity, c, db) for c in cols}
    return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "entity": entity, "cols": cols, "item": item, "errors": {}, "field_meta": field_meta, "form_values": {}, "view_only": True})


@app.post("/admin/server-maintenance")
async def server_maintenance(request: Request, db: Session = Depends(get_db), user=Depends(require_admin)):
    global DRAWING_DIR, PDF_DIR, PART_FILE_DIR, RUNTIME_SETTINGS
    form = await request.form()
    action = form.get("action", "")
    chosen_branch = (form.get("branch") or "").replace("remotes/origin/", "", 1).strip()
    message = "No action taken"

    if action in {"refresh_branches", "switch_branch", "pull_latest"} and not run_git_command(["--version"]):
        message = "Git is not available on this server. Install git to use branch maintenance actions."
    elif action == "refresh_branches":
        fetch_result = run_git_command(["fetch", "--all", "--prune"])
        message = "Branch list refreshed" if fetch_result and fetch_result.returncode == 0 else f"Refresh failed: {(fetch_result.stderr.strip() if fetch_result else 'git unavailable')}"
    elif action == "switch_branch" and chosen_branch:
        run_git_command(["fetch", "origin", chosen_branch])
        checkout_result = run_git_command(["checkout", chosen_branch])
        if checkout_result and checkout_result.returncode != 0:
            tracking_result = run_git_command(["checkout", "-B", chosen_branch, f"origin/{chosen_branch}"])
            checkout_result = tracking_result or checkout_result
        if not checkout_result:
            message = "Git is not available on this server."
        else:
            message = "Branch switched" if checkout_result.returncode == 0 else f"Branch switch failed: {checkout_result.stderr.strip()}"
    elif action == "pull_latest":
        pull_branch = chosen_branch
        if not pull_branch:
            branch_lookup = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"])
            pull_branch = (branch_lookup.stdout.strip() if branch_lookup else "")
        if not pull_branch:
            message = "Unable to determine branch for pull."
        else:
            run_git_command(["fetch", "origin", pull_branch])
            run_git_command(["checkout", pull_branch])
            result = run_git_command(["pull", "origin", pull_branch])
            if not result:
                message = "Unable to run git pull on this server."
            else:
                if result.returncode != 0:
                    message = f"Pull failed: {result.stderr.strip()}"
                else:
                    applied, apply_message = run_post_pull_command()
                    if applied:
                        message = "Latest changes pulled and reload command queued."
                    else:
                        message = f"Latest changes pulled. {apply_message}."
    elif action == "update_paths":
        DRAWING_DIR = Path(form.get("DRAWING_DATA_PATH", str(DRAWING_DIR)))
        PDF_DIR = Path(form.get("PDF_DATA_PATH", str(PDF_DIR)))
        PART_FILE_DIR = Path(form.get("PART_FILE_DATA_PATH", str(PART_FILE_DIR)))
        sql_data_path = str(form.get("SQL_DATA_PATH", RUNTIME_SETTINGS.get("SQL_DATA_PATH") or os.getenv("SQL_DATA_PATH", "/data/sql/mts.db"))).strip()
        DRAWING_DIR.mkdir(parents=True, exist_ok=True)
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        PART_FILE_DIR.mkdir(parents=True, exist_ok=True)
        Path(sql_data_path).parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_SETTINGS.update({
            "DRAWING_DATA_PATH": str(DRAWING_DIR),
            "PDF_DATA_PATH": str(PDF_DIR),
            "PART_FILE_DATA_PATH": str(PART_FILE_DIR),
            "SQL_DATA_PATH": sql_data_path,
        })
        persisted = save_runtime_settings(RUNTIME_SETTINGS)
        message = f"Data paths saved to {SETTINGS_PATH}. Restart app to apply DB path changes." if persisted else "Failed to persist settings to disk."

    return RedirectResponse(f"/admin?tab=server-maintenance&message={message}", status_code=302)


@app.post("/entity/employees/{item_id}/password")
async def employee_change_password(item_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_admin)):
    form = await request.form()
    new_password = (form.get("new_password") or "").strip()
    confirm_password = (form.get("confirm_password") or "").strip()

    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if new_password != confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    employee = db.query(models.Employee).filter_by(id=item_id).first()
    if not employee:
        raise HTTPException(404)

    employee.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse(f"/entity/employees/{item_id}/edit", status_code=302)


@app.get("/entity/{entity}/new", response_class=HTMLResponse)
def entity_new(entity: str, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    if not can_write(user, entity):
        raise HTTPException(403)
    model = MODEL_MAP.get(entity)
    cols = [c for c in model.__table__.columns if c.name != "id"]
    field_meta = {c.name: build_field_meta(entity, c, db) for c in cols}
    return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "entity": entity, "cols": cols, "item": None, "errors": {}, "field_meta": field_meta, "form_values": {}})


@app.post("/entity/{entity}/save")
async def entity_save(entity: str, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    if not can_write(user, entity):
        raise HTTPException(403)
    model = MODEL_MAP.get(entity)
    if not model:
        raise HTTPException(404)

    form = await request.form()
    item_id = form.get("id")
    item = db.query(model).filter_by(id=int(item_id)).first() if item_id else model()
    cols = [c for c in model.__table__.columns if c.name != "id"]
    field_meta = {c.name: build_field_meta(entity, c, db) for c in cols}
    errors = {}
    values = {}

    for col in model.__table__.columns:
        if col.name == "id":
            continue
        if col.name in form:
            raw_val = form.get(col.name)
            values[col.name] = raw_val
            try:
                parsed = parse_field_value(entity, col, raw_val)
            except ValueError as exc:
                errors[col.name] = str(exc)
                continue

            if parsed is None and field_meta[col.name]["required"]:
                errors[col.name] = "This field is required"
                continue

            setattr(item, col.name, parsed)

    if errors:
        return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "entity": entity, "cols": cols, "item": item if item_id else None, "errors": errors, "field_meta": field_meta, "form_values": values}, status_code=422)

    if not item_id:
        db.add(item)
    try:
        db.commit()
        db.refresh(item)
    except IntegrityError as exc:
        db.rollback()
        details = str(exc.orig) if getattr(exc, "orig", None) else str(exc)
        friendly = "Could not save record because one or more fields have invalid or duplicate data."
        return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "entity": entity, "cols": cols, "item": item if item_id else None, "errors": {"__all__": f"{friendly} ({details})"}, "field_meta": field_meta, "form_values": values}, status_code=422)
    except SQLAlchemyError:
        db.rollback()
        return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "entity": entity, "cols": cols, "item": item if item_id else None, "errors": {"__all__": "Unexpected database error while saving. Please review values and try again."}, "field_meta": field_meta, "form_values": values}, status_code=500)

    if entity == "pallets":
        snapshot = {"status": item.status, "station": item.current_station_id, "at": datetime.utcnow().isoformat()}
        rev = models.PalletRevision(pallet_id=item.id, revision_code=f"R{int(datetime.utcnow().timestamp())}", snapshot_json=json.dumps(snapshot), created_by=user.username)
        db.add(rev)
        db.commit()
        create_traveler_file(db, item.id)
    if entity == "cut_sheet_revisions":
        item.pdf_path = str(PDF_DIR / f"cut_sheet_{item.id}_{item.revision_code}.pdf")
        db.commit()
    if entity == "maintenance_requests":
        if not item.requested_by:
            item.requested_by = user.username
        if not item.requested_user_id:
            item.requested_user_id = user.id
        if not item.status:
            item.status = "submitted"
        db.commit()
    return RedirectResponse(f"/entity/{entity}", status_code=302)


@app.get("/entity/{entity}/{item_id}/edit", response_class=HTMLResponse)
def entity_edit(entity: str, item_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    model = MODEL_MAP.get(entity)
    item = db.query(model).filter_by(id=item_id).first()
    cols = [c for c in model.__table__.columns if c.name != "id"]
    field_meta = {c.name: build_field_meta(entity, c, db) for c in cols}
    return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "entity": entity, "cols": cols, "item": item, "errors": {}, "field_meta": field_meta, "form_values": {}})


@app.post("/entity/{entity}/{item_id}/delete")
def entity_delete(entity: str, item_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    if not can_write(user, entity):
        raise HTTPException(403)
    model = MODEL_MAP.get(entity)
    item = db.query(model).filter_by(id=item_id).first()
    if item:
        if entity == "pallets" and item.production_order_id:
            order = db.query(models.ProductionOrder).filter_by(id=item.production_order_id).first()
            if order and order.status not in {"cancelled", "complete", "closed"}:
                order.status = "cancelled"
        if entity == "pallets":
            clear_pallet_storage_bin(db, item)
            rollback_inventory_for_deleted_pallet(db, item)
            db.query(models.Queue).filter_by(pallet_id=item.id).delete(synchronize_session=False)
            db.query(models.PalletEvent).filter_by(pallet_id=item.id).delete(synchronize_session=False)
            db.query(models.PalletPart).filter_by(pallet_id=item.id).delete(synchronize_session=False)
            db.query(models.PalletStationRoute).filter_by(pallet_id=item.id).delete(synchronize_session=False)
            db.query(models.PalletRevision).filter_by(pallet_id=item.id).delete(synchronize_session=False)
        db.delete(item)
        db.commit()
    return RedirectResponse(f"/entity/{entity}", status_code=302)


@app.post("/pallets/{pallet_id}/split")
async def split_pallet(pallet_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    source = db.query(models.Pallet).filter_by(id=pallet_id).first()
    if not source:
        raise HTTPException(404)
    form = await request.form()
    qty = float(form.get("quantity", 0))
    child = models.Pallet(pallet_code=f"{source.pallet_code}-S{int(datetime.utcnow().timestamp())}", pallet_type="split", parent_pallet_id=source.id, status=source.status, created_by=user.username)
    db.add(child)
    db.commit()
    parts = db.query(models.PalletPart).filter_by(pallet_id=source.id).all()
    for p in parts:
        moved = min(qty, p.actual_quantity)
        p.actual_quantity -= moved
        db.add(models.PalletPart(pallet_id=child.id, part_revision_id=p.part_revision_id, planned_quantity=moved, actual_quantity=moved))
    db.commit()
    create_traveler_file(db, child.id)
    return RedirectResponse(f"/entity/pallets", status_code=302)


@app.post("/pallets/combine")
async def combine_pallets(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    form = await request.form()
    target_id = int(form.get("target_id"))
    source_id = int(form.get("source_id"))
    target = db.query(models.Pallet).filter_by(id=target_id).first()
    source = db.query(models.Pallet).filter_by(id=source_id).first()
    if not target or not source:
        raise HTTPException(404)
    source_parts = db.query(models.PalletPart).filter_by(pallet_id=source.id).all()
    for sp in source_parts:
        tp = db.query(models.PalletPart).filter_by(pallet_id=target.id, part_revision_id=sp.part_revision_id).first()
        if tp:
            tp.actual_quantity += sp.actual_quantity
        else:
            db.add(models.PalletPart(pallet_id=target.id, part_revision_id=sp.part_revision_id, planned_quantity=sp.planned_quantity, actual_quantity=sp.actual_quantity))
    source.status = "combined"
    db.commit()
    create_traveler_file(db, target.id)
    return RedirectResponse("/entity/pallets", status_code=302)


def create_traveler_file(db: Session, pallet_id: int):
    pallet = db.query(models.Pallet).filter_by(id=pallet_id).first()
    parts = db.query(models.PalletPart).filter_by(pallet_id=pallet_id).all()
    lines = [f"Traveler - Pallet {pallet.pallet_code}", f"Status: {pallet.status}", f"Generated: {datetime.utcnow().isoformat()}", "", "Parts:"]
    for p in parts:
        lines.append(f"Part Revision {p.part_revision_id}: qty {p.actual_quantity}")
    out = PDF_DIR / f"traveler_{pallet.pallet_code}.txt"
    out.write_text("\n".join(lines))


def _require_cutplan_write(user):
    if user.role not in ("admin", "planner") and not can_write(user, "parts"):
        raise HTTPException(status_code=403)


def cutplan_storage_root() -> Path:
    root = Path("data")
    (root / "mpf").mkdir(parents=True, exist_ok=True)
    (root / "gen").mkdir(parents=True, exist_ok=True)
    return root


NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)"
RE_FLOATS = re.compile(NUM)
RE_X = re.compile(r"\bX(" + NUM + r")\b", re.I)
RE_Y = re.compile(r"\bY(" + NUM + r")\b", re.I)
RE_I = re.compile(r"\bI(" + NUM + r")\b", re.I)
RE_J = re.compile(r"\bJ(" + NUM + r")\b", re.I)


def _extract_call_floats(line: str, keyword: str) -> list[float]:
    m = re.search(rf"{keyword}\(([^)]*)\)", line, re.I)
    if not m:
        return []
    return [float(v) for v in RE_FLOATS.findall(m.group(1))]


def _arc_points(start, end, i, j, cw: bool, step_deg: float = 6.0):
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
    return [[cx + r * math.cos(a0 + total * (k / n)), cy + r * math.sin(a0 + total * (k / n))] for k in range(n + 1)]


def parse_hk_mpf(text: str) -> dict:
    x = y = 0.0
    cut_on = False
    sheet = {"width": None, "height": None}
    parts = []
    current_part = None
    current_contour = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        u = line.upper()
        if u.startswith("HKINI"):
            vals = _extract_call_floats(u, "HKINI")
            if len(vals) >= 3:
                sheet["width"] = vals[1]
                sheet["height"] = vals[2]
            continue
        if "HKOST(" in u:
            vals = _extract_call_floats(u, "HKOST")
            current_part = {"program_id": int(vals[3]) if len(vals) >= 4 else None, "tech": int(vals[4]) if len(vals) >= 5 else None, "contours": []}
            parts.append(current_part)
            continue
        if "HKSTR(" in u:
            vals = _extract_call_floats(u, "HKSTR")
            # HKSTR args 3/4/5 are contour start offsets on the sheet (X/Y/Z).
            # Use X/Y as the active tool position so subsequent moves render
            # relative to the contour's actual sheet start.
            x = vals[2] if len(vals) >= 3 else x
            y = vals[3] if len(vals) >= 4 else y
            current_contour = {"type": "outer" if (int(vals[0]) if vals else 0) == 0 else "hole", "hkstr": vals, "segments": []}
            if current_part is None:
                current_part = {"program_id": None, "tech": None, "contours": []}
                parts.append(current_part)
            current_part["contours"].append(current_contour)
            continue
        if "HKCUT" in u:
            cut_on = True
            continue
        if "HKSTO" in u:
            cut_on = False
            current_contour = None
            continue
        if "HKPED" in u:
            current_part = None
            current_contour = None
            cut_on = False
            continue
        if u.startswith("WHEN") or not cut_on or current_contour is None:
            continue
        if u.startswith("G1"):
            nx = float(RE_X.search(u).group(1)) if RE_X.search(u) else x
            ny = float(RE_Y.search(u).group(1)) if RE_Y.search(u) else y
            current_contour["segments"].append({"kind": "line", "a": [x, y], "b": [nx, ny]})
            x, y = nx, ny
            continue
        if u.startswith("G2") or u.startswith("G3"):
            mx, my, mi, mj = RE_X.search(u), RE_Y.search(u), RE_I.search(u), RE_J.search(u)
            if not (mx and my and mi and mj):
                continue
            nx, ny = float(mx.group(1)), float(my.group(1))
            current_contour["segments"].append({"kind": "polyline", "points": _arc_points((x, y), (nx, ny), float(mi.group(1)), float(mj.group(1)), cw=u.startswith("G2"))})
            x, y = nx, ny
    sheet["width"] = float(sheet["width"] or 0.0)
    sheet["height"] = float(sheet["height"] or 0.0)
    contour_id = 1
    for part in parts:
        for contour in part["contours"]:
            contour["id"] = contour_id
            contour_id += 1
    return {"sheet": sheet, "parts": parts}


def _contour_to_ring(contour: dict, tol: float = 1e-4):
    pts = []
    for seg in contour["segments"]:
        if seg["kind"] == "line":
            if not pts:
                pts.append(seg["a"])
            pts.append(seg["b"])
        elif seg["kind"] == "polyline":
            if not pts:
                pts.extend(seg["points"])
            elif pts[-1] == seg["points"][0]:
                pts.extend(seg["points"][1:])
            else:
                pts.extend(seg["points"])
    if len(pts) < 4:
        return None
    if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) > tol:
        pts.append(pts[0])
    return pts


def compute_skeleton(model: dict) -> dict:
    try:
        from shapely.geometry import LineString, Polygon
        from shapely.ops import unary_union
    except ModuleNotFoundError as exc:
        raise HTTPException(status_code=500, detail="Shapely dependency is not installed.") from exc

    def parts_to_polygons() -> list:
        out = []
        for part in model["parts"]:
            outers, holes = [], []
            for contour in part["contours"]:
                ring = _contour_to_ring(contour)
                if not ring:
                    continue
                poly = Polygon(ring).buffer(0)
                if poly.is_empty:
                    continue
                (outers if contour["type"] == "outer" else holes).append(poly)
            if not outers:
                continue
            outer_union = unary_union(outers).buffer(0)
            hole_union = unary_union([hole for hole in holes if hole.within(outer_union)]).buffer(0) if holes else None
            part_poly = outer_union.difference(hole_union).buffer(0) if hole_union else outer_union
            if not part_poly.is_empty:
                out.append(part_poly)
        return out

    width = model["sheet"]["width"]
    height = model["sheet"]["height"]
    sheet_poly = Polygon([(0, 0), (width, 0), (width, height), (0, height)]).buffer(0)
    part_polys = parts_to_polygons()
    parts_union = unary_union(part_polys).buffer(0) if part_polys else Polygon()
    skeleton = sheet_poly.difference(parts_union).buffer(0)

    candidates = [
        LineString([(0, y), (width, y)]) for y in (height * 0.25, height * 0.5, height * 0.75)
    ] + [
        LineString([(x, 0), (x, height)]) for x in (width / 3.0, width * 2.0 / 3.0)
    ]
    cut_lines = []
    for line in candidates:
        clipped = line.intersection(skeleton)
        if clipped.is_empty:
            continue
        clipped = clipped.difference(parts_union).buffer(0)
        if clipped.is_empty:
            continue
        if clipped.geom_type == "LineString":
            cut_lines.append(clipped)
        elif clipped.geom_type == "MultiLineString":
            cut_lines.extend([geom for geom in clipped.geoms if geom.length > 1e-4])

    model2 = dict(model)
    skeleton_cuts = []
    for idx, line in enumerate(cut_lines, start=1):
        coords = list(line.coords)
        skeleton_cuts.append({"id": idx, "a": [coords[0][0], coords[0][1]], "b": [coords[-1][0], coords[-1][1]]})
    model2["skeletonCuts"] = skeleton_cuts
    return model2


def export_reordered_mpf(original_text: str, order: list[int]) -> str:
    lines = original_text.splitlines()
    blocks, preamble, postamble = [], [], []
    in_block = False
    current = []
    seen_any = False
    for line in lines:
        u = line.strip().upper()
        if "HKSTR(" in u:
            in_block = True
            seen_any = True
            current = [line]
            continue
        if in_block:
            current.append(line)
            if "HKSTO" in u:
                blocks.append(current)
                in_block = False
                current = []
            continue
        if not seen_any:
            preamble.append(line)
        else:
            postamble.append(line)
    if len(order) != len(blocks):
        raise HTTPException(status_code=400, detail=f"order length {len(order)} != blocks {len(blocks)}")
    reordered = preamble
    for contour_id in order:
        reordered.extend(blocks[contour_id - 1])
    reordered.extend(postamble)
    return "\n".join(reordered)


def generate_skeleton_mpf(original_text: str, model_with_skeleton: dict) -> str:
    lines = original_text.splitlines()
    insert_at = len(lines)
    for i, line in enumerate(lines):
        if "HKEND" in line.upper() or line.strip().upper().startswith("M30"):
            insert_at = i
            break
    out = lines[:insert_at]
    out.append("N900000 HKOST(0.0,0.0,0.0,990001,99,0,0,0)")
    seq = 900010
    for cut in model_with_skeleton.get("skeletonCuts", []):
        ax, ay = cut["a"]
        bx, by = cut["b"]
        out.append(f"N{seq} HKSTR(0,1,{ax:.4f},{ay:.4f},0,0,0,0)")
        seq += 10
        out.extend(["HKPIE(0,0,0)", "HKLEA(0,0,0)", "HKCUT(0,0,0)", f"G1 X{ax:.4f} Y{ay:.4f}", f"G1 X{bx:.4f} Y{by:.4f}", "HKSTO(0,0,0)"])
    out.append(f"N{seq} HKPED(0,0,0)")
    out.extend(lines[insert_at:])
    return "\n".join(out)


def _render_cutplan_index(request: Request, db: Session, user):
    jobs = db.query(models.CutJob).order_by(models.CutJob.created_at.desc()).limit(50).all()
    mpf_rows = db.query(models.MpfMaster).order_by(models.MpfMaster.created_at.desc()).limit(200).all()
    return templates.TemplateResponse(
        "cutplan/index.html",
        {"request": request, "user": user, "jobs": jobs, "mpf_rows": mpf_rows, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, **engineering_nav_context()},
    )


def _render_cutplan_view(job_id: int, request: Request, db: Session, user):
    job = db.query(models.CutJob).filter(models.CutJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    return templates.TemplateResponse(
        "cutplan/view.html",
        {"request": request, "user": user, "job": job, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, **engineering_nav_context()},
    )


@app.get("/cutplan", response_class=HTMLResponse)
def cutplan_index(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return _render_cutplan_index(request, db, user)


@app.get("/engineering/hk-mpf/cutplanner", response_class=HTMLResponse)
def engineering_cutplan_index(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return _render_cutplan_index(request, db, user)


@app.post("/cutplan/upload")
async def cutplan_upload(
    request: Request,
    file: UploadFile | None = File(None),
    name: str = Form(""),
    engineering_job_id: str | None = Form(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    _require_cutplan_write(user)
    source_job_id: int | None = None
    if engineering_job_id not in (None, ""):
        try:
            source_job_id = int(engineering_job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Source HK MPF must be a valid integer.") from exc

    source_job = None
    source_file_path: Path | None = None
    if source_job_id is not None:
        source_job = db.query(models.MpfMaster).filter_by(id=source_job_id).first()
        if not source_job:
            raise HTTPException(status_code=404, detail="Selected Source HK MPF was not found.")
        source_candidates = sorted(
            PART_FILE_DIR.glob(f"*_{source_job.mpf_filename}"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if source_candidates:
            source_file_path = source_candidates[0]
        else:
            direct_path = PART_FILE_DIR / source_job.mpf_filename
            if direct_path.exists():
                source_file_path = direct_path

    if (not file or not file.filename) and source_file_path is None:
        raise HTTPException(status_code=400, detail="Upload an MPF file or choose Source HK MPF.")

    root = cutplan_storage_root()
    source_name = file.filename if file and file.filename else (source_job.mpf_filename if source_job else "upload.mpf")
    mpf_path = root / "mpf" / f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{Path(source_name).name}"
    if file and file.filename:
        content = await file.read()
    else:
        content = source_file_path.read_bytes() if source_file_path else b""
    mpf_path.write_bytes(content)
    parsed = parse_hk_mpf(content.decode("utf-8", errors="ignore"))
    clean_name = (name or "").strip()
    if not clean_name:
        if source_job:
            clean_name = f"CutPlan - {source_job.mpf_filename}"
        elif source_name:
            clean_name = f"CutPlan - {Path(source_name).name}"
        else:
            clean_name = "CutPlan Job"
    job = models.CutJob(name=clean_name, mpf_path=str(mpf_path), engineering_job_id=source_job_id)
    db.add(job)
    db.flush()
    db.add(models.CutArtifact(job_id=job.id, kind="parsed", json_text=json.dumps(parsed)))
    db.commit()
    return RedirectResponse(url=f"/engineering/hk-mpf/cutplanner/{job.id}", status_code=303)


@app.post("/engineering/hk-mpf/cutplanner/upload")
async def engineering_cutplan_upload(
    request: Request,
    file: UploadFile | None = File(None),
    name: str = Form(""),
    engineering_job_id: str | None = Form(None),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    return await cutplan_upload(request=request, file=file, name=name, engineering_job_id=engineering_job_id, db=db, user=user)


@app.get("/cutplan/{job_id}", response_class=HTMLResponse)
def cutplan_view(job_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return _render_cutplan_view(job_id, request, db, user)


@app.get("/engineering/hk-mpf/cutplanner/{job_id}", response_class=HTMLResponse)
def engineering_cutplan_view(job_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return _render_cutplan_view(job_id, request, db, user)


@app.get("/api/cutplan/{job_id}/model")
def api_cutplan_model(job_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    art = db.query(models.CutArtifact).filter(models.CutArtifact.job_id == job_id, models.CutArtifact.kind == "parsed").order_by(models.CutArtifact.created_at.desc()).first()
    if not art:
        raise HTTPException(404, "Parsed model not found")
    return JSONResponse(json.loads(art.json_text))


@app.post("/api/cutplan/{job_id}/reorder")
async def api_cutplan_reorder(job_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    _require_cutplan_write(user)
    payload = await request.json()
    order = payload.get("order")
    if not isinstance(order, list) or not all(isinstance(v, int) for v in order):
        raise HTTPException(400, "order must be list[int]")
    job = db.query(models.CutJob).filter(models.CutJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    original = Path(job.mpf_path).read_text(encoding="utf-8", errors="ignore")
    out_path = cutplan_storage_root() / "gen" / f"job_{job.id}_reordered.mpf"
    out_path.write_text(export_reordered_mpf(original, order), encoding="utf-8")
    db.add(models.CutArtifact(job_id=job.id, kind="reordered", file_path=str(out_path), json_text=json.dumps({"order": order})))
    db.commit()
    return JSONResponse({"ok": True, "download": f"/cutplan/{job_id}/download/reordered"})


@app.post("/api/cutplan/{job_id}/compute_skeleton")
def api_compute_skeleton(job_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    _require_cutplan_write(user)
    job = db.query(models.CutJob).filter(models.CutJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    parsed_art = db.query(models.CutArtifact).filter(models.CutArtifact.job_id == job_id, models.CutArtifact.kind == "parsed").order_by(models.CutArtifact.created_at.desc()).first()
    if not parsed_art:
        raise HTTPException(404, "Parsed model not found")
    model2 = compute_skeleton(json.loads(parsed_art.json_text))
    original = Path(job.mpf_path).read_text(encoding="utf-8", errors="ignore")
    out_path = cutplan_storage_root() / "gen" / f"job_{job.id}_skeleton.mpf"
    out_path.write_text(generate_skeleton_mpf(original, model2), encoding="utf-8")
    db.add(models.CutArtifact(job_id=job.id, kind="skeleton", json_text=json.dumps(model2), file_path=str(out_path)))
    db.commit()
    return JSONResponse({"ok": True, "download": f"/cutplan/{job_id}/download/skeleton"})


@app.get("/cutplan/{job_id}/download/{kind}")
def cutplan_download(job_id: int, kind: str, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    if kind not in ("reordered", "skeleton"):
        raise HTTPException(400, "Invalid kind")
    art = db.query(models.CutArtifact).filter(models.CutArtifact.job_id == job_id, models.CutArtifact.kind == kind).order_by(models.CutArtifact.created_at.desc()).first()
    if not art or not art.file_path:
        raise HTTPException(404, "File not found")
    return FileResponse(art.file_path, filename=os.path.basename(art.file_path))


@app.get("/engineering/hk-mpf/cutplanner/{job_id}/download/{kind}")
def engineering_cutplan_download(job_id: int, kind: str, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return cutplan_download(job_id=job_id, kind=kind, request=request, db=db, user=user)
