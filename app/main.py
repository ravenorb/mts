import json
import os
from datetime import datetime
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
REVISION_UPLOAD_DIR = Path(os.getenv("REVISION_UPLOAD_PATH", "/data/revision_files"))
DRAWING_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)
REVISION_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MODEL_MAP = {
    "parts": models.Part,
    "part_revisions": models.PartRevision,
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
    "consumables": models.Consumable,
    "purchase_requests": models.PurchaseRequest,
    "purchase_request_lines": models.PurchaseRequestLine,
    "consumable_usage_logs": models.ConsumableUsageLog,
    "employees": models.Employee,
    "skills": models.Skill,
    "employee_skills": models.EmployeeSkill,
    "users": models.User,
    "part_revision_files": models.PartRevisionFile,
    "engineering_questions": models.EngineeringQuestion,
    "station_activities": models.StationActivity,
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
    ("maintenance_requests", "status"): ["open", "in_progress", "closed"],
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
    ("Maintenance", "/entity/maintenance_requests"),
    ("Admin", "/entity/users"),
]

ENTITY_GROUPS = {
    "Production": ["pallets", "pallet_parts", "pallet_events", "queues", "production_orders"],
    "Engineering": ["parts", "part_revisions", "part_process_definitions", "part_revision_files", "engineering_questions", "cut_sheets", "cut_sheet_revisions", "cut_sheet_revision_outputs", "boms"],
    "Maintenance": ["maintenance_requests", "station_maintenance_tasks"],
    "Inventory": ["consumables", "consumable_usage_logs", "purchase_request_lines"],
    "People": ["employees", "skills", "employee_skills", "users", "station_activities"],
}


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
    maintenance_open = db.query(models.MaintenanceRequest).filter_by(status="open").count()
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


ALLOWED_REVISION_FILE_TYPES = {
    "laser": {".nc", ".dxf", ".txt", ".tap"},
    "waterjet": {".nc", ".dxf", ".txt"},
    "welder_module": {".mod", ".prg", ".txt", ".zip"},
    "drawing": {".dwg", ".dxf", ".step", ".stp"},
    "pdf": {".pdf"},
}


def _save_revision_file(part_revision_id: int, upload: UploadFile) -> tuple[str, str]:
    suffix = Path(upload.filename or "").suffix.lower()
    out_dir = REVISION_UPLOAD_DIR / f"revision_{part_revision_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{int(datetime.utcnow().timestamp())}_{Path(upload.filename or 'file').name}"
    target = out_dir / safe_name
    with target.open("wb") as fh:
        fh.write(upload.file.read())
    return suffix, str(target)


@app.get("/engineering", response_class=HTMLResponse)
def engineering_dashboard(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    parts_without_revision = (
        db.query(models.Part)
        .outerjoin(models.PartRevision, models.PartRevision.part_id == models.Part.id)
        .filter(models.PartRevision.id.is_(None))
        .order_by(models.Part.created_at.desc())
        .all()
    )
    open_questions = db.query(models.EngineeringQuestion).filter_by(status="open").order_by(models.EngineeringQuestion.created_at.desc()).all()
    recent_revisions = db.query(models.PartRevision).order_by(models.PartRevision.released_at.desc()).limit(20).all()
    return templates.TemplateResponse("engineering_dashboard.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "parts_without_revision": parts_without_revision, "open_questions": open_questions, "recent_revisions": recent_revisions})


@app.get("/engineering/revision/{part_revision_id}", response_class=HTMLResponse)
def engineering_revision_detail(part_revision_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login), message: str | None = None, error: str | None = None):
    revision = db.query(models.PartRevision).filter_by(id=part_revision_id).first()
    if not revision:
        raise HTTPException(404)
    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    files = db.query(models.PartRevisionFile).filter_by(part_revision_id=part_revision_id).order_by(models.PartRevisionFile.uploaded_at.desc()).all()
    return templates.TemplateResponse("engineering_revision_detail.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "revision": revision, "stations": stations, "files": files, "file_types": ALLOWED_REVISION_FILE_TYPES.keys(), "message": message, "error": error})


@app.post("/engineering/revision/{part_revision_id}/upload", response_class=HTMLResponse)
def engineering_upload_save(part_revision_id: int, request: Request, file_type: str = Form(...), available_station_ids: list[str] = Form([]), upload_file: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(require_login)):
    if file_type not in ALLOWED_REVISION_FILE_TYPES:
        return engineering_revision_detail(part_revision_id, request, db, user, error="Unknown file type selected.")
    if not upload_file.filename:
        return engineering_revision_detail(part_revision_id, request, db, user, error="Please choose a file to upload.")

    suffix, saved_path = _save_revision_file(part_revision_id, upload_file)
    if suffix not in ALLOWED_REVISION_FILE_TYPES[file_type]:
        Path(saved_path).unlink(missing_ok=True)
        allowed = ", ".join(sorted(ALLOWED_REVISION_FILE_TYPES[file_type]))
        return engineering_revision_detail(part_revision_id, request, db, user, error=f"Invalid extension {suffix or '(none)'} for {file_type}. Allowed: {allowed}")

    file_row = models.PartRevisionFile(
        part_revision_id=part_revision_id,
        file_type=file_type,
        file_name=upload_file.filename,
        file_path=saved_path,
        station_ids_csv=",".join(available_station_ids),
        uploaded_by=user.username,
    )
    db.add(file_row)

    process = db.query(models.PartProcessDefinition).filter_by(part_revision_id=part_revision_id).first()
    if not process:
        process = models.PartProcessDefinition(part_revision_id=part_revision_id)
        db.add(process)

    if file_type == "laser":
        process.laser_required = True
        process.laser_program_path = saved_path
    elif file_type == "waterjet":
        process.waterjet_required = True
        process.waterjet_program_path = saved_path
    elif file_type == "welder_module":
        process.robotic_weld_required = True
        process.robotic_weld_program_path = saved_path
    elif file_type == "drawing":
        process.manual_weld_required = True
        process.manual_weld_drawing_path = saved_path

    db.commit()
    return engineering_revision_detail(part_revision_id, request, db, user, message="Revision file uploaded and availability saved.")


@app.get("/engineering/upload", response_class=HTMLResponse)
def engineering_upload_page_redirect():
    return RedirectResponse("/engineering", status_code=302)


@app.get("/engineering/machine-program", response_class=HTMLResponse)
def engineering_machine_program_stub(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return templates.TemplateResponse("engineering_machine_program_stub.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS})


@app.get("/stations", response_class=HTMLResponse)
def stations_home(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    return templates.TemplateResponse("stations_home.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "stations": stations})


@app.get("/stations/{station_id}", response_class=HTMLResponse)
def station_dashboard(station_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login), message: str | None = None):
    station = db.query(models.Station).filter_by(id=station_id, active=True).first()
    if not station:
        raise HTTPException(404)
    queued = db.query(models.Queue).filter_by(station_id=station_id).order_by(models.Queue.queue_position.asc()).all()
    next_queue = queued[0] if queued else None
    pallets = db.query(models.Pallet).filter_by(current_station_id=station_id).order_by(models.Pallet.created_at.desc()).limit(10).all()
    consumables = db.query(models.Consumable).order_by(models.Consumable.description.asc()).limit(100).all()
    activities = db.query(models.StationActivity).filter_by(station_id=station_id).order_by(models.StationActivity.created_at.desc()).limit(20).all()
    return templates.TemplateResponse("station_dashboard.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "station": station, "queued": queued, "next_queue": next_queue, "pallets": pallets, "consumables": consumables, "activities": activities, "message": message})


def _station_activity(db: Session, station_id: int, activity_type: str, employee_code: str = "", notes: str = "", pallet_id: int | None = None):
    db.add(models.StationActivity(station_id=station_id, activity_type=activity_type, employee_code=employee_code, notes=notes, pallet_id=pallet_id))


@app.post("/stations/{station_id}/login")
def station_login(station_id: int, employee_code: str = Form(...), password: str = Form(...), db: Session = Depends(get_db), user=Depends(require_login)):
    _station_activity(db, station_id, "station_login", employee_code=employee_code, notes="Station login acknowledged")
    db.commit()
    return RedirectResponse(f"/stations/{station_id}?message=Operator+login+captured", status_code=302)


@app.post("/stations/{station_id}/break")
def station_break(station_id: int, employee_code: str = Form(...), notes: str = Form(""), db: Session = Depends(get_db), user=Depends(require_login)):
    _station_activity(db, station_id, "break_start", employee_code=employee_code, notes=notes)
    db.commit()
    return RedirectResponse(f"/stations/{station_id}?message=Break+logged", status_code=302)


@app.post("/stations/{station_id}/end-shift")
def station_end_shift(station_id: int, employee_code: str = Form(...), notes: str = Form(""), db: Session = Depends(get_db), user=Depends(require_login)):
    _station_activity(db, station_id, "end_shift", employee_code=employee_code, notes=notes)
    db.commit()
    return RedirectResponse(f"/stations/{station_id}?message=Shift+end+logged", status_code=302)


@app.post("/stations/{station_id}/start-pallet")
def station_start_pallet(station_id: int, pallet_code: str = Form(...), employee_code: str = Form(...), db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = db.query(models.Pallet).filter_by(pallet_code=pallet_code).first()
    if not pallet:
        raise HTTPException(404, "Pallet not found")
    pallet.current_station_id = station_id
    pallet.status = "in_progress"
    _station_activity(db, station_id, "start_work", employee_code=employee_code, pallet_id=pallet.id, notes=f"Started work on {pallet_code}")
    db.add(models.PalletEvent(pallet_id=pallet.id, station_id=station_id, event_type="start_work", quantity=0, recorded_by=user.username, notes=f"Operator {employee_code} started work"))
    db.commit()
    return RedirectResponse(f"/stations/{station_id}?message=Started+work+on+{pallet_code}", status_code=302)


@app.post("/stations/{station_id}/stop-pallet")
def station_stop_pallet(station_id: int, pallet_code: str = Form(...), work_completed: float = Form(0), scrap_qty: float = Form(0), split_qty: float = Form(0), combine_source_code: str = Form(""), employee_code: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = db.query(models.Pallet).filter_by(pallet_code=pallet_code).first()
    if not pallet:
        raise HTTPException(404, "Pallet not found")
    db.add(models.PalletEvent(pallet_id=pallet.id, station_id=station_id, event_type="stop_work", quantity=work_completed, recorded_by=user.username, notes=f"Completed {work_completed}, scrap {scrap_qty}. {notes}"))
    if scrap_qty > 0:
        db.add(models.PalletEvent(pallet_id=pallet.id, station_id=station_id, event_type="scrapped", quantity=scrap_qty, recorded_by=user.username, notes="Scrap reported from station dashboard"))
    if split_qty > 0:
        child = models.Pallet(pallet_code=f"{pallet.pallet_code}-S{int(datetime.utcnow().timestamp())}", pallet_type="split", parent_pallet_id=pallet.id, status=pallet.status, current_station_id=station_id, created_by=user.username)
        db.add(child)
        db.flush()
        db.add(models.PalletEvent(pallet_id=child.id, station_id=station_id, event_type="split", quantity=split_qty, recorded_by=user.username, notes=f"Split from {pallet.pallet_code}"))
    if combine_source_code:
        source = db.query(models.Pallet).filter_by(pallet_code=combine_source_code).first()
        if source and source.id != pallet.id:
            source.status = "combined"
            db.add(models.PalletEvent(pallet_id=pallet.id, station_id=station_id, event_type="merged", quantity=0, recorded_by=user.username, notes=f"Merged source pallet {combine_source_code}"))
    _station_activity(db, station_id, "stop_work", employee_code=employee_code, pallet_id=pallet.id, notes=notes or f"Stopped work on {pallet_code}")
    db.commit()
    return RedirectResponse(f"/stations/{station_id}?message=Stop+work+recorded+for+{pallet_code}", status_code=302)


@app.post("/stations/{station_id}/maintenance")
def station_report_maintenance(station_id: int, issue_description: str = Form(...), priority: str = Form("normal"), employee_code: str = Form(""), db: Session = Depends(get_db), user=Depends(require_login)):
    db.add(models.MaintenanceRequest(station_id=station_id, requested_by=employee_code or user.username, priority=priority, status="open", issue_description=issue_description))
    _station_activity(db, station_id, "maintenance_report", employee_code=employee_code, notes=issue_description)
    db.commit()
    return RedirectResponse(f"/stations/{station_id}?message=Maintenance+problem+reported", status_code=302)


@app.post("/stations/{station_id}/engineering-issue")
def station_report_engineering(station_id: int, question_text: str = Form(...), part_revision_id: int | None = Form(None), employee_code: str = Form(""), db: Session = Depends(get_db), user=Depends(require_login)):
    db.add(models.EngineeringQuestion(station_id=station_id, part_revision_id=part_revision_id, question_text=question_text, asked_by=employee_code or user.username, status="open"))
    _station_activity(db, station_id, "engineering_issue", employee_code=employee_code, notes=question_text)
    db.commit()
    return RedirectResponse(f"/stations/{station_id}?message=Engineering+issue+reported", status_code=302)


@app.post("/stations/{station_id}/consumable-usage")
def station_consumable_usage(station_id: int, consumable_id: int = Form(...), quantity_delta: float = Form(...), reason: str = Form(...), employee_code: str = Form(""), db: Session = Depends(get_db), user=Depends(require_login)):
    db.add(models.ConsumableUsageLog(consumable_id=consumable_id, station_id=station_id, quantity_delta=quantity_delta, reason=reason))
    _station_activity(db, station_id, "consumable_usage", employee_code=employee_code, notes=f"Consumable {consumable_id}: {quantity_delta}")
    db.commit()
    return RedirectResponse(f"/stations/{station_id}?message=Consumable+usage+logged", status_code=302)


@app.post("/stations/{station_id}/consumable-reorder")
def station_consumable_reorder(station_id: int, consumable_id: int = Form(...), quantity: float = Form(...), employee_code: str = Form(""), db: Session = Depends(get_db), user=Depends(require_login)):
    pr = models.PurchaseRequest(requested_by=employee_code or user.username, status="open")
    db.add(pr)
    db.flush()
    db.add(models.PurchaseRequestLine(purchase_request_id=pr.id, consumable_id=consumable_id, quantity=quantity))
    _station_activity(db, station_id, "consumable_reorder", employee_code=employee_code, notes=f"PR {pr.id} consumable {consumable_id} qty {quantity}")
    db.commit()
    return RedirectResponse(f"/stations/{station_id}?message=Reorder+request+submitted", status_code=302)


@app.get("/stations/{station_id}/manage-pallets", response_class=HTMLResponse)
def station_manage_pallets(station_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    station = db.query(models.Station).filter_by(id=station_id, active=True).first()
    if not station:
        raise HTTPException(404)
    pallets = db.query(models.Pallet).order_by(models.Pallet.created_at.desc()).limit(50).all()
    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    return templates.TemplateResponse("station_manage_pallets.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "station": station, "pallets": pallets, "stations": stations})


@app.post("/stations/{station_id}/split-pallet")
def station_split_pallet(station_id: int, pallet_id: int = Form(...), quantity: float = Form(...), db: Session = Depends(get_db), user=Depends(require_login)):
    source = db.query(models.Pallet).filter_by(id=pallet_id).first()
    if not source:
        raise HTTPException(404)
    child = models.Pallet(pallet_code=f"{source.pallet_code}-S{int(datetime.utcnow().timestamp())}", pallet_type="split", parent_pallet_id=source.id, status=source.status, current_station_id=source.current_station_id, created_by=user.username)
    db.add(child)
    db.flush()
    source_parts = db.query(models.PalletPart).filter_by(pallet_id=source.id).all()
    for part in source_parts:
        moved = min(quantity, part.actual_quantity)
        part.actual_quantity -= moved
        db.add(models.PalletPart(pallet_id=child.id, part_revision_id=part.part_revision_id, planned_quantity=moved, actual_quantity=moved, scrap_quantity=0))
    db.add(models.PalletEvent(pallet_id=source.id, station_id=source.current_station_id, event_type="split", quantity=quantity, recorded_by=user.username, notes=f"Split to {child.pallet_code}"))
    db.commit()
    return RedirectResponse(f"/stations/{station_id}/manage-pallets", status_code=302)


@app.post("/stations/{station_id}/update-location")
def station_update_location(station_id: int, pallet_id: int = Form(...), new_station_id: int = Form(...), db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = db.query(models.Pallet).filter_by(id=pallet_id).first()
    if not pallet:
        raise HTTPException(404)
    pallet.current_station_id = new_station_id
    db.add(models.PalletEvent(pallet_id=pallet.id, station_id=new_station_id, event_type="moved", quantity=0, recorded_by=user.username, notes=f"Location updated from station manage screen"))
    db.commit()
    return RedirectResponse(f"/stations/{station_id}/manage-pallets", status_code=302)


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
