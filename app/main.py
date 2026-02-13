import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
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

DRAWING_DIR = Path(os.getenv("DRAWING_DATA_PATH", "/data/drawings"))
PDF_DIR = Path(os.getenv("PDF_DATA_PATH", "/data/pdfs"))
PART_FILE_DIR = Path(os.getenv("PART_FILE_DATA_PATH", "/data/part_revision_files"))
DRAWING_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)
PART_FILE_DIR.mkdir(parents=True, exist_ok=True)


def run_git_command(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None

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
    "employees": models.Employee,
    "skills": models.Skill,
    "employee_skills": models.EmployeeSkill,
    "users": models.User,
}

ROLE_WRITE = {
    "operator": {"pallets", "pallet_parts", "pallet_events", "queues"},
    "maintenance": {"maintenance_requests", "station_maintenance_tasks", "pallet_events"},
    "purchasing": {"consumables", "purchase_requests", "purchase_request_lines", "consumable_usage_logs"},
    "planner": set(MODEL_MAP.keys()) - {"users"},
    "admin": set(MODEL_MAP.keys()),
}

FIELD_CHOICES = {
    ("users", "role"): ["operator", "maintenance", "purchasing", "planner", "admin"],
    ("employees", "role"): ["operator", "maintenance", "purchasing", "planner", "admin"],
    ("part_revisions", "is_current"): ["true", "false"],
    ("cut_sheet_revisions", "is_current"): ["true", "false"],
    ("stations", "active"): ["true", "false"],
    ("pallets", "status"): ["staged", "in_progress", "hold", "complete", "combined"],
    ("pallets", "pallet_type"): ["manual", "split", "mixed"],
    ("queues", "status"): ["queued", "in_progress", "blocked", "done"],
    ("maintenance_requests", "priority"): ["low", "normal", "high", "urgent"],
    ("maintenance_requests", "status"): ["submitted", "reviewed", "scheduled", "waiting on parts", "complete"],
    ("purchase_requests", "status"): ["open", "approved", "ordered", "received", "closed"],
    ("engineering_questions", "status"): ["open", "answered", "closed"],
}

TOP_NAV = [
    ("Dashboard", "/"),
    ("Production", "/production"),
    ("Engineering", "/engineering"),
    ("Stations", "/stations"),
    ("Inventory", "/entity/consumables"),
    ("Purchasing", "/entity/purchase_requests"),
    ("Maintenance", "/maintenance"),
    ("Admin", "/admin"),
]

ENTITY_GROUPS = {
    "Production": ["pallets", "pallet_parts", "pallet_events", "queues", "production_orders"],
    "Engineering": ["parts", "part_revisions", "part_revision_files", "engineering_questions", "part_process_definitions", "cut_sheets", "cut_sheet_revisions", "cut_sheet_revision_outputs", "boms"],
    "Maintenance": ["maintenance_requests", "station_maintenance_tasks", "maintenance_logs"],
    "Inventory": ["consumables", "consumable_usage_logs", "purchase_request_lines"],
    "People": ["employees", "skills", "employee_skills", "users"],
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
    if not db.query(models.User).filter_by(username="admin").first():
        db.add(models.User(username="admin", password_hash=hash_password("admin123"), role="admin"))
        db.commit()


def get_current_user(request: Request, db: Session):
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.query(models.User).filter_by(id=uid, active=True).first()


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
    create_default_admin(db)


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
    missing_revisions = db.query(models.Part).outerjoin(models.PartRevision, models.PartRevision.part_id == models.Part.id).filter(models.PartRevision.id.is_(None)).order_by(models.Part.created_at.desc()).all()
    open_questions = db.query(models.EngineeringQuestion).filter_by(status="open").order_by(models.EngineeringQuestion.created_at.desc()).limit(30).all()
    latest_files = db.query(models.PartRevisionFile).order_by(models.PartRevisionFile.uploaded_at.desc()).limit(20).all()
    return templates.TemplateResponse("engineering_dashboard.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "missing_revisions": missing_revisions, "open_questions": open_questions, "latest_files": latest_files})


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
    return templates.TemplateResponse("engineering_machine_program_stub.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS})


@app.get("/stations", response_class=HTMLResponse)
def stations_dashboard(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    queue = db.query(models.Queue).filter(models.Queue.status.in_(["queued", "in_progress"])).order_by(models.Queue.queue_position.asc()).limit(30).all()
    return templates.TemplateResponse("stations_dashboard.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "stations": stations, "queue": queue})


@app.get("/stations/login", response_class=HTMLResponse)
def stations_login(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return templates.TemplateResponse("station_login.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "error": None, "ok": None})


@app.post("/stations/login", response_class=HTMLResponse)
def stations_login_submit(request: Request, station_user_id: str = Form(...), station_password: str = Form(...), db: Session = Depends(get_db), user=Depends(require_login)):
    ok = bool(station_user_id.strip()) and bool(station_password.strip())
    return templates.TemplateResponse("station_login.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "error": None if ok else "Missing credentials", "ok": "Station login accepted (stub)." if ok else None})


@app.post("/stations/report-engineering-issue")
def stations_report_engineering_issue(station_id: int = Form(...), pallet_id: int | None = Form(None), question_text: str = Form(...), db: Session = Depends(get_db), user=Depends(require_login)):
    db.add(models.EngineeringQuestion(station_id=station_id, pallet_id=pallet_id, asked_by=user.username, question_text=question_text, status="open"))
    db.commit()
    return RedirectResponse("/stations", status_code=302)


@app.get("/maintenance", response_class=HTMLResponse)
def maintenance_dashboard(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    ensure_upcoming_scheduled_requests(db)
    legacy_rows = db.query(models.MaintenanceRequest).filter(models.MaintenanceRequest.status.in_(list(LEGACY_MAINTENANCE_STATUS_MAP.keys()))).all()
    for row in legacy_rows:
        normalize_maintenance_status(row)
    if legacy_rows:
        db.commit()
    open_requests = db.query(models.MaintenanceRequest).filter(
        models.MaintenanceRequest.request_type == "request",
        models.MaintenanceRequest.status.in_(MAINTENANCE_ACTIVE_STATUSES),
    ).order_by(models.MaintenanceRequest.created_at.desc()).all()
    upcoming = db.query(models.MaintenanceRequest).filter(
        models.MaintenanceRequest.request_type == "scheduled",
        models.MaintenanceRequest.status.in_(MAINTENANCE_ACTIVE_STATUSES),
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
    })


@app.get("/maintenance/{request_id}", response_class=HTMLResponse)
def maintenance_request_detail(request_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    maint = db.query(models.MaintenanceRequest).filter_by(id=request_id).first()
    if not maint:
        raise HTTPException(404)
    normalize_maintenance_status(maint)
    db.commit()
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


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(models.User).filter_by(username=username, active=True).first()
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
def admin_dashboard(request: Request, tab: str = "users", db: Session = Depends(get_db), user=Depends(require_admin)):
    tab = tab if tab in {"users", "stations", "skills", "employees", "server-maintenance"} else "users"
    tab_data = {
        "users": db.query(models.User).order_by(models.User.id.desc()).limit(200).all(),
        "stations": db.query(models.Station).order_by(models.Station.id.desc()).limit(200).all(),
        "skills": db.query(models.Skill).order_by(models.Skill.id.desc()).limit(200).all(),
        "employees": db.query(models.Employee).order_by(models.Employee.id.desc()).limit(200).all(),
    }

    branch_result = run_git_command(["branch", "--list"])
    branch_lines = branch_result.stdout.splitlines() if branch_result else []
    branches = [line.replace("*", "").strip() for line in branch_lines if line.strip()]
    active_branch = next((line.replace("*", "").strip() for line in branch_lines if line.startswith("*")), "")

    admin_cols = {k: [c.name for c in MODEL_MAP[k].__table__.columns] for k in ["users", "stations", "skills", "employees"]}

    return templates.TemplateResponse("admin_dashboard.html", {
        "request": request,
        "user": user,
        "top_nav": TOP_NAV,
        "entity_groups": ENTITY_GROUPS,
        "active_tab": tab,
        "tab_data": tab_data,
        "admin_cols": admin_cols,
        "branches": branches,
        "active_branch": active_branch,
        "data_paths": {
            "DRAWING_DATA_PATH": str(DRAWING_DIR),
            "PDF_DATA_PATH": str(PDF_DIR),
            "PART_FILE_DATA_PATH": str(PART_FILE_DIR),
        },
        "message": request.query_params.get("message"),
    })


@app.get("/admin/{entity}/{item_id}/view", response_class=HTMLResponse)
def admin_entity_view(entity: str, item_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_admin)):
    if entity not in {"employees", "stations", "skills", "users"}:
        raise HTTPException(404)
    model = MODEL_MAP.get(entity)
    item = db.query(model).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(404)
    cols = [c for c in model.__table__.columns if c.name != "id"]
    field_meta = {c.name: build_field_meta(entity, c, db) for c in cols}
    return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "entity": entity, "cols": cols, "item": item, "errors": {}, "field_meta": field_meta, "form_values": {}, "view_only": True, "linked_user": db.query(models.User).filter_by(id=item.user_id).first() if entity == "employees" and item and item.user_id else None})


@app.post("/admin/server-maintenance")
async def server_maintenance(request: Request, db: Session = Depends(get_db), user=Depends(require_admin)):
    global DRAWING_DIR, PDF_DIR, PART_FILE_DIR
    form = await request.form()
    action = form.get("action", "")
    chosen_branch = form.get("branch", "")
    message = "No action taken"

    if action in {"switch_branch", "pull_latest"} and not run_git_command(["--version"]):
        message = "Git is not available on this server. Install git to use branch maintenance actions."
    elif action == "switch_branch" and chosen_branch:
        result = run_git_command(["checkout", chosen_branch])
        if not result:
            message = "Git is not available on this server."
        else:
            message = "Branch switched" if result.returncode == 0 else f"Branch switch failed: {result.stderr.strip()}"
    elif action == "pull_latest":
        branch_lookup = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"])
        pull_branch = chosen_branch or (branch_lookup.stdout.strip() if branch_lookup else "")
        result = run_git_command(["pull", "origin", pull_branch]) if pull_branch else None
        if not result:
            message = "Unable to determine branch or run git pull on this server."
        else:
            message = "Latest changes pulled" if result.returncode == 0 else f"Pull failed: {result.stderr.strip()}"
    elif action == "update_paths":
        DRAWING_DIR = Path(form.get("DRAWING_DATA_PATH", str(DRAWING_DIR)))
        PDF_DIR = Path(form.get("PDF_DATA_PATH", str(PDF_DIR)))
        PART_FILE_DIR = Path(form.get("PART_FILE_DATA_PATH", str(PART_FILE_DIR)))
        DRAWING_DIR.mkdir(parents=True, exist_ok=True)
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        PART_FILE_DIR.mkdir(parents=True, exist_ok=True)
        message = "Data paths updated for running server"

    return RedirectResponse(f"/admin?tab=server-maintenance&message={message}", status_code=302)


@app.get("/entity/{entity}/new", response_class=HTMLResponse)
def entity_new(entity: str, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    if not can_write(user, entity):
        raise HTTPException(403)
    model = MODEL_MAP.get(entity)
    cols = [c for c in model.__table__.columns if c.name != "id"]
    field_meta = {c.name: build_field_meta(entity, c, db) for c in cols}
    return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "entity": entity, "cols": cols, "item": None, "errors": {}, "field_meta": field_meta, "form_values": {}, "linked_user": None})


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

    if entity == "employees":
        logon_username = (form.get("logon_username") or "").strip()
        logon_password = (form.get("logon_password") or "").strip()
        if logon_username:
            linked_user = db.query(models.User).filter_by(username=logon_username).first()
            if linked_user and item.user_id and linked_user.id != item.user_id:
                return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "entity": entity, "cols": cols, "item": item if item_id else None, "errors": {"__all__": "Selected logon is already linked to another employee."}, "field_meta": field_meta, "form_values": values}, status_code=422)
            if not linked_user:
                linked_user = models.User(username=logon_username, password_hash=hash_password(logon_password or "change-me"), role=item.role or "operator", active=item.active)
                db.add(linked_user)
                db.commit()
                db.refresh(linked_user)
            elif logon_password:
                linked_user.password_hash = hash_password(logon_password)
                linked_user.role = item.role or linked_user.role
                linked_user.active = item.active
                db.commit()
            item.user_id = linked_user.id
            db.commit()

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
    return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "entity": entity, "cols": cols, "item": item, "errors": {}, "field_meta": field_meta, "form_values": {}, "linked_user": db.query(models.User).filter_by(id=item.user_id).first() if entity == "employees" and item and item.user_id else None})


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
