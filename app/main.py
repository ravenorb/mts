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

from . import models
from .auth import hash_password, verify_password
from .database import Base, engine, get_db

app = FastAPI(title="Manufacturing Tracking System")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "change-me"))
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

PDF_DIR = Path(os.getenv("PDF_DATA_PATH", "/data/pdfs"))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DATA_PATH", "/data/uploads"))
PDF_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MODEL_MAP = {
    "parts": models.Part,
    "part_revisions": models.PartRevision,
    "part_process_definitions": models.PartProcessDefinition,
    "part_revision_files": models.PartRevisionFile,
    "part_revision_file_stations": models.PartRevisionFileStation,
    "engineering_questions": models.EngineeringQuestion,
    "station_work_logs": models.StationWorkLog,
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
}

ROLE_WRITE = {
    "operator": {"pallets", "pallet_parts", "pallet_events", "queues", "station_work_logs", "engineering_questions", "consumable_usage_logs"},
    "maintenance": {"maintenance_requests", "station_maintenance_tasks", "pallet_events", "station_work_logs"},
    "purchasing": {"consumables", "purchase_requests", "purchase_request_lines", "consumable_usage_logs"},
    "planner": set(MODEL_MAP.keys()) - {"users"},
    "admin": set(MODEL_MAP.keys()),
}

FIELD_CHOICES = {
    ("users", "role"): ["operator", "maintenance", "purchasing", "planner", "admin", "engineer"],
    ("employees", "role"): ["operator", "maintenance", "purchasing", "planner", "admin", "engineer"],
    ("part_revisions", "is_current"): ["true", "false"],
    ("cut_sheet_revisions", "is_current"): ["true", "false"],
    ("stations", "active"): ["true", "false"],
    ("pallets", "status"): ["staged", "in_progress", "hold", "complete", "combined"],
    ("pallets", "pallet_type"): ["manual", "split", "mixed"],
    ("queues", "status"): ["queued", "in_progress", "blocked", "done"],
    ("maintenance_requests", "priority"): ["low", "normal", "high", "urgent"],
    ("maintenance_requests", "status"): ["open", "in_progress", "closed"],
    ("purchase_requests", "status"): ["open", "approved", "ordered", "received", "closed"],
    ("engineering_questions", "status"): ["open", "resolved"],
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
    "Stations": ["stations", "station_work_logs"],
    "Maintenance": ["maintenance_requests", "station_maintenance_tasks"],
    "Inventory": ["consumables", "consumable_usage_logs", "purchase_request_lines"],
    "People": ["employees", "skills", "employee_skills", "users"],
}

ALLOWED_UPLOAD_TYPES = {
    "laser": [".nc", ".dxf", ".dwg", ".txt", ".tap"],
    "waterjet": [".nc", ".dxf", ".dwg", ".txt"],
    "welder": [".mod", ".src", ".txt", ".zip"],
    "drawing": [".dwg", ".dxf", ".step", ".stp", ".sldprt", ".sldasm"],
    "pdf": [".pdf"],
}


def get_current_user(request: Request, db: Session):
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.query(models.User).filter_by(id=uid, active=True).first()


def create_default_admin(db: Session):
    if db.query(models.User).count() == 0:
        db.add(models.User(username="admin", password_hash=hash_password("admin123"), role="admin", active=True))
        db.commit()


def require_login(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401)
    return user


def can_write(user, entity):
    return entity in ROLE_WRITE.get(user.role, set())


def fk_choices(col, db: Session):
    fk = next(iter(col.foreign_keys), None)
    if not fk:
        return None
    table_name = fk.column.table.name
    label_columns = ["pallet_code", "station_name", "part_number", "revision_code", "cut_sheet_number", "username", "employee_code", "description", "name"]
    for _, model in MODEL_MAP.items():
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
        expected = "Whole number"
    elif isinstance(col.type, Float):
        expected = "Number"
    elif isinstance(col.type, Boolean):
        expected = "Choose true or false"
    elif isinstance(col.type, DateTime):
        expected = "ISO datetime"
    elif isinstance(col.type, String):
        expected = f"Text up to {col.type.length}" if col.type.length else "Text"
    elif isinstance(col.type, Text):
        expected = "Long text"

    required = (not col.nullable) and col.default is None and col.server_default is None
    return {"required": required, "expected": expected, "choices": choices, "fk_choices": fk_choices(col, db)}


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
        return int(val)
    if isinstance(col.type, Float):
        return float(val)
    if isinstance(col.type, DateTime):
        return datetime.fromisoformat(str(val))
    return str(val)


def create_traveler_file(db: Session, pallet_id: int):
    pallet = db.query(models.Pallet).filter_by(id=pallet_id).first()
    parts = db.query(models.PalletPart).filter_by(pallet_id=pallet_id).all()
    lines = [f"Traveler - Pallet {pallet.pallet_code}", f"Status: {pallet.status}", f"Generated: {datetime.utcnow().isoformat()}", "", "Parts:"]
    for p in parts:
        lines.append(f"Part Revision {p.part_revision_id}: qty {p.actual_quantity}")
    (PDF_DIR / f"traveler_{pallet.pallet_code}.txt").write_text("\n".join(lines))


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
    staged = db.query(models.Pallet).filter(models.Pallet.status == "staged").count()
    in_progress = db.query(models.Pallet).filter(models.Pallet.status == "in_progress").count()
    maintenance_open = db.query(models.MaintenanceRequest).filter_by(status="open").count()
    low_stock = db.query(models.Consumable).filter(models.Consumable.qty_on_hand <= models.Consumable.reorder_point).count()
    station_rows = db.query(models.Station.id, models.Station.station_name, func.count(models.Queue.id)).outerjoin(models.Queue, models.Queue.station_id == models.Station.id).group_by(models.Station.id, models.Station.station_name).all()
    max_load = max([r[2] for r in station_rows], default=1)
    station_load = [{"id": r[0], "name": r[1], "load": r[2], "percent": int((r[2] / max_load) * 100) if max_load else 0} for r in station_rows]
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "active": active, "hold": hold, "staged": staged, "in_progress": in_progress, "station_load": station_load, "maintenance_open": maintenance_open, "low_stock": low_stock})


@app.get("/engineering", response_class=HTMLResponse)
def engineering_dashboard(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    pending_parts = db.query(models.Part).outerjoin(models.PartRevision, models.Part.id == models.PartRevision.part_id).filter(models.PartRevision.id.is_(None)).order_by(models.Part.created_at.desc()).all()
    open_questions = db.query(models.EngineeringQuestion).filter_by(status="open").order_by(models.EngineeringQuestion.created_at.asc()).limit(100).all()
    part_revisions = db.query(models.PartRevision).order_by(models.PartRevision.id.desc()).limit(200).all()
    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    files = db.query(models.PartRevisionFile).order_by(models.PartRevisionFile.uploaded_at.desc()).limit(200).all()
    station_links = db.query(models.PartRevisionFileStation).all()
    station_name = {s.id: s.station_name for s in stations}
    available_map = {}
    for link in station_links:
        available_map.setdefault(link.part_revision_file_id, []).append(station_name.get(link.station_id, str(link.station_id)))
    return templates.TemplateResponse("engineering_dashboard.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "pending_parts": pending_parts, "open_questions": open_questions, "part_revisions": part_revisions, "stations": stations, "files": files, "available_map": available_map, "message": request.query_params.get("message")})


@app.post("/engineering/revision-file")
async def upload_revision_file(
    part_revision_id: int = Form(...),
    file_type: str = Form(...),
    station_ids: list[int] = Form([]),
    upload: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(require_login),
):
    if file_type not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(422, "Unsupported file type")
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix and suffix not in ALLOWED_UPLOAD_TYPES[file_type]:
        raise HTTPException(422, f"File extension {suffix} is not allowed for {file_type}")

    rev_folder = UPLOAD_DIR / f"part_revision_{part_revision_id}"
    rev_folder.mkdir(parents=True, exist_ok=True)
    safe_name = f"{int(datetime.utcnow().timestamp())}_{Path(upload.filename or 'upload.bin').name}"
    destination = rev_folder / safe_name
    destination.write_bytes(await upload.read())

    record = models.PartRevisionFile(part_revision_id=part_revision_id, file_type=file_type, file_name=upload.filename or safe_name, stored_path=str(destination), uploaded_by=user.username)
    db.add(record)
    db.commit()
    db.refresh(record)
    for station_id in station_ids:
        db.add(models.PartRevisionFileStation(part_revision_file_id=record.id, station_id=station_id))
    db.commit()

    process = db.query(models.PartProcessDefinition).filter_by(part_revision_id=part_revision_id).first()
    if not process:
        process = models.PartProcessDefinition(part_revision_id=part_revision_id)
        db.add(process)
    if file_type == "laser":
        process.laser_required, process.laser_program_path = True, str(destination)
    elif file_type == "waterjet":
        process.waterjet_required, process.waterjet_program_path = True, str(destination)
    elif file_type == "welder":
        process.robotic_weld_required, process.robotic_weld_program_path = True, str(destination)
    elif file_type == "drawing":
        process.manual_weld_required, process.manual_weld_drawing_path = True, str(destination)
    db.commit()

    return RedirectResponse("/engineering?message=Revision+file+uploaded", status_code=302)


@app.get("/engineering/machine-programs", response_class=HTMLResponse)
def machine_program_todo(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    return templates.TemplateResponse("machine_program_todo.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS})


@app.post("/engineering/questions/{question_id}/resolve")
def resolve_question(question_id: int, db: Session = Depends(get_db), user=Depends(require_login)):
    q = db.query(models.EngineeringQuestion).filter_by(id=question_id).first()
    if q:
        q.status = "resolved"
        q.resolved_by = user.username
        q.resolved_at = datetime.utcnow()
        db.commit()
    return RedirectResponse("/engineering", status_code=302)


@app.get("/stations", response_class=HTMLResponse)
def station_directory(request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    return templates.TemplateResponse("stations.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "stations": stations})


@app.get("/stations/{station_id}", response_class=HTMLResponse)
def station_dashboard(station_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    station = db.query(models.Station).filter_by(id=station_id).first()
    if not station:
        raise HTTPException(404)
    queue = db.query(models.Queue).filter_by(station_id=station_id).order_by(models.Queue.priority_score.desc(), models.Queue.id.asc()).all()
    next_pallet = queue[0] if queue else None
    logs = db.query(models.StationWorkLog).filter_by(station_id=station_id).order_by(models.StationWorkLog.logged_at.desc()).limit(20).all()
    return templates.TemplateResponse("station_dashboard.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "station": station, "queue": queue, "next_pallet": next_pallet, "logs": logs, "message": request.query_params.get("message")})


@app.post("/stations/{station_id}/action")
async def station_action(station_id: int, request: Request, action: str = Form(...), pallet_code: str = Form(""), notes: str = Form(""), quantity_completed: float = Form(0), quantity_scrap: float = Form(0), combine_with_pallet_code: str = Form(""), split_to_pallet_code: str = Form(""), db: Session = Depends(get_db), user=Depends(require_login)):
    station = db.query(models.Station).filter_by(id=station_id).first()
    if not station:
        raise HTTPException(404)

    pallet = db.query(models.Pallet).filter_by(pallet_code=pallet_code).first() if pallet_code else None
    db.add(models.StationWorkLog(
        station_id=station_id,
        pallet_id=pallet.id if pallet else None,
        event_type=action,
        notes=notes,
        quantity_completed=quantity_completed,
        quantity_scrap=quantity_scrap,
        combine_with_pallet_code=combine_with_pallet_code,
        split_to_pallet_code=split_to_pallet_code,
        logged_by=user.username,
    ))

    if action in {"start_work", "stop_work"} and pallet:
        db.add(models.PalletEvent(pallet_id=pallet.id, station_id=station_id, event_type=action, quantity=quantity_completed, recorded_by=user.username, notes=notes))

    if action == "engineering_issue":
        db.add(models.EngineeringQuestion(station_id=station_id, pallet_id=pallet.id if pallet else None, question_text=notes or "Engineering issue reported", created_by=user.username, status="open"))

    if action == "maintenance_problem":
        db.add(models.MaintenanceRequest(station_id=station_id, requested_by=user.username, issue_description=notes or "Maintenance issue reported", priority="normal", status="open"))

    if action == "request_reorder":
        consumable = db.query(models.Consumable).order_by(models.Consumable.id.asc()).first()
        if consumable:
            req = models.PurchaseRequest(requested_by=user.username, status="open")
            db.add(req)
            db.flush()
            db.add(models.PurchaseRequestLine(purchase_request_id=req.id, consumable_id=consumable.id, quantity=max(quantity_completed, 1)))

    db.commit()
    return RedirectResponse(f"/stations/{station_id}?message=Action+saved", status_code=302)


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
    return templates.TemplateResponse("production.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "query": q, "found": pallet, "next_pallets": next_pallets, "stations": stations, "part_revisions": part_revisions})


@app.get("/production/pallet/{pallet_id}", response_class=HTMLResponse)
def pallet_detail(pallet_id: int, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    pallet = db.query(models.Pallet).filter_by(id=pallet_id).first()
    if not pallet:
        raise HTTPException(404)
    parts = db.query(models.PalletPart).filter_by(pallet_id=pallet_id).all()
    events = db.query(models.PalletEvent).filter_by(pallet_id=pallet_id).order_by(models.PalletEvent.recorded_at.asc()).all()
    stations = db.query(models.Station).filter_by(active=True).order_by(models.Station.station_name.asc()).all()
    return templates.TemplateResponse("pallet_detail.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "pallet": pallet, "parts": parts, "events": events, "stations": stations})


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
    values = {k: form.get(k) for k in form.keys()}
    item_id = values.get("id")
    item = db.query(model).filter_by(id=int(item_id)).first() if item_id else model()
    cols = [c for c in model.__table__.columns if c.name != "id"]
    field_meta = {c.name: build_field_meta(entity, c, db) for c in cols}

    errors = {}
    for col in cols:
        if col.name in values:
            raw = values.get(col.name)
            try:
                parsed = parse_field_value(entity, col, raw)
            except Exception as exc:
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
        return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "entity": entity, "cols": cols, "item": item if item_id else None, "errors": {"__all__": f"Could not save record ({details})"}, "field_meta": field_meta, "form_values": values}, status_code=422)
    except SQLAlchemyError:
        db.rollback()
        return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "top_nav": TOP_NAV, "entity_groups": ENTITY_GROUPS, "entity": entity, "cols": cols, "item": item if item_id else None, "errors": {"__all__": "Unexpected database error while saving."}, "field_meta": field_meta, "form_values": values}, status_code=500)

    if entity == "pallets":
        snapshot = {"status": item.status, "station": item.current_station_id, "at": datetime.utcnow().isoformat()}
        db.add(models.PalletRevision(pallet_id=item.id, revision_code=f"R{int(datetime.utcnow().timestamp())}", snapshot_json=json.dumps(snapshot), created_by=user.username))
        db.commit()
        create_traveler_file(db, item.id)
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
