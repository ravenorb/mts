import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func, text
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
    ("pallets", "status"): ["staged", "in_progress", "hold", "complete", "combined"],
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


@app.get("/production", response_class=HTMLResponse)
def production(request: Request, q: str = "", db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = None
    if q:
        pallet_query = db.query(models.Pallet).filter(models.Pallet.pallet_code == q)
        if q.isdigit():
            pallet_query = db.query(models.Pallet).filter((models.Pallet.pallet_code == q) | (models.Pallet.id == int(q)))
        pallet = pallet_query.first()
    next_pallets = db.query(models.Pallet).filter(models.Pallet.status.in_(["staged", "in_progress", "hold"])).order_by(models.Pallet.created_at.desc()).limit(12).all()
    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    part_revisions = db.query(models.PartRevision).order_by(models.PartRevision.id.desc()).limit(200).all()
    return templates.TemplateResponse("production.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "query": q, "found": pallet, "next_pallets": next_pallets, "stations": stations, "part_revisions": part_revisions, "errors": {}})


@app.get("/production/pallet/{pallet_id}", response_class=HTMLResponse)
def pallet_detail(pallet_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = db.query(models.Pallet).filter_by(id=pallet_id).first()
    if not pallet:
        raise HTTPException(404)
    parts = db.query(models.PalletPart).filter_by(pallet_id=pallet_id).all()
    events = db.query(models.PalletEvent).filter_by(pallet_id=pallet_id).order_by(models.PalletEvent.recorded_at.asc()).all()
    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    return templates.TemplateResponse("pallet_detail.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "pallet": pallet, "parts": parts, "events": events, "stations": stations, "errors": {}})


@app.post("/production/pallet/{pallet_id}/move")
def pallet_move(pallet_id: int, station_id: int = Form(...), notes: str = Form(""), db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = db.query(models.Pallet).filter_by(id=pallet_id).first()
    station = db.query(models.Station).filter_by(id=station_id).first()
    if not pallet or not station:
        raise HTTPException(404)
    pallet.current_station_id = station_id
    pallet.status = "in_progress"
    db.add(models.PalletEvent(pallet_id=pallet.id, station_id=station_id, event_type="moved", quantity=0, recorded_by=user.username, notes=notes or f"Moved to {station.station_name}"))
    db.commit()
    return RedirectResponse(f"/production/pallet/{pallet.id}", status_code=302)


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
    db.add(models.PalletEvent(pallet_id=pallet.id, station_id=location_station_id, event_type="created", quantity=quantity, recorded_by=user.username, notes="Manual pallet creation"))
    db.commit()
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


@app.get("/engineering/parts/{part_id}", response_class=HTMLResponse)
def engineering_part_detail(part_id: str, request: Request, mode: str = "view", rev_id: int | None = None, db: Session = Depends(get_db), user=Depends(require_login)):
    part = db.query(models.PartMaster).filter_by(part_id=part_id).first()
    if not part:
        raise HTTPException(404)
    selected_rev = rev_id if rev_id is not None else part.cur_rev
    bom_lines = db.query(models.RevisionBom).filter_by(part_id=part_id, rev_id=selected_rev).order_by(models.RevisionBom.id.asc()).all()
    revision_header = db.query(models.RevisionHeader).filter_by(part_id=part_id, rev_id=selected_rev).first()
    revision_list = db.query(models.RevisionHeader.rev_id).filter_by(part_id=part_id).order_by(models.RevisionHeader.rev_id.desc()).all()
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
        **engineering_nav_context(),
    })


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
    return templates.TemplateResponse("engineering_hk_mpfs.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, **engineering_nav_context()})


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
    dwg_match = re.search(r"DWG\s*#\s*[:\-]?\s*([A-Z0-9\-_.]+)", page2, re.IGNORECASE)
    if dwg_match:
        primary_part_id = dwg_match.group(1).strip()

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


@app.post("/engineering/hk-mpfs/parse")
async def engineering_hk_mpf_parse(mpf_file: UploadFile = File(...), pdf_file: UploadFile = File(...), user=Depends(require_login)):
    if not mpf_file.filename:
        raise HTTPException(status_code=400, detail="MPF file is required.")
    if not pdf_file.filename or not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="PDF file is required.")

    pdf_bytes = await pdf_file.read()
    parsed = parse_hk_cutsheet(pdf_bytes)
    return JSONResponse(parsed)


@app.get("/engineering/wj-gcode", response_class=HTMLResponse)
def engineering_wj_gcode_page(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return templates.TemplateResponse("engineering_machine_program_stub.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "page_title": "WJ Gcode", "page_message": "WJ Gcode dashboard is coming next.", **engineering_nav_context()})


@app.get("/engineering/abb-modules", response_class=HTMLResponse)
def engineering_abb_modules_page(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return templates.TemplateResponse("engineering_machine_program_stub.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "page_title": "ABB Modules", "page_message": "ABB module dashboard is coming next.", **engineering_nav_context()})


@app.get("/engineering/pdfs", response_class=HTMLResponse)
def engineering_pdfs_page(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return templates.TemplateResponse("engineering_machine_program_stub.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "page_title": "PDFs", "page_message": "PDF dashboard is coming next.", **engineering_nav_context()})


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
    queue = [{"id": q.id, "position": q.queue_position, "status": q.status, "pallet": db.query(models.Pallet).filter_by(id=q.pallet_id).first()} for q in queue_rows]
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
            db.add(models.PalletEvent(pallet_id=pallet.id, station_id=station_id, event_type="started", quantity=0, recorded_by=user.username, notes="Started next queued pallet"))
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
                db.add(models.StorageBin(storage_location_id=location.id, shelf_id=shelf_id, bin_id=bin_id))
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
    return templates.TemplateResponse("storage_locations.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "rows": rows})


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
    return RedirectResponse("/inventory/locations", status_code=302)


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
    return RedirectResponse("/inventory/locations", status_code=302)


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
    row.pallet_id = (form.get("pallet_id") or "").strip()
    row.part_number = (form.get("part_number") or "").strip()
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
                message = "Latest changes pulled. Rebuild/restart container to apply runtime changes." if result.returncode == 0 else f"Pull failed: {result.stderr.strip()}"
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
