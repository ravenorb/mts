import json
import os
from datetime import datetime
from pathlib import Path
from fastapi import Depends, FastAPI, Form, HTTPException, Request
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
DRAWING_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)

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
}


def build_field_meta(entity: str, col):
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
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "active": active, "hold": hold, "bottlenecks": bottlenecks, "maintenance_open": maintenance_open, "low_stock": low_stock})


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
    return templates.TemplateResponse("entity_list.html", {"request": request, "user": user, "entity": entity, "rows": rows, "cols": cols, "can_write": can_write(user, entity)})


@app.get("/entity/{entity}/new", response_class=HTMLResponse)
def entity_new(entity: str, request: Request, db: Session = Depends(get_db), user=Depends(require_login)):
    if not can_write(user, entity):
        raise HTTPException(403)
    model = MODEL_MAP.get(entity)
    cols = [c for c in model.__table__.columns if c.name != "id"]
    field_meta = {c.name: build_field_meta(entity, c) for c in cols}
    return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "entity": entity, "cols": cols, "item": None, "errors": {}, "field_meta": field_meta, "form_values": {}})


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
    field_meta = {c.name: build_field_meta(entity, c) for c in cols}
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
        return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "entity": entity, "cols": cols, "item": item if item_id else None, "errors": errors, "field_meta": field_meta, "form_values": values}, status_code=422)

    if not item_id:
        db.add(item)
    try:
        db.commit()
        db.refresh(item)
    except IntegrityError as exc:
        db.rollback()
        details = str(exc.orig) if getattr(exc, "orig", None) else str(exc)
        friendly = "Could not save record because one or more fields have invalid or duplicate data."
        return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "entity": entity, "cols": cols, "item": item if item_id else None, "errors": {"__all__": f"{friendly} ({details})"}, "field_meta": field_meta, "form_values": values}, status_code=422)
    except SQLAlchemyError:
        db.rollback()
        return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "entity": entity, "cols": cols, "item": item if item_id else None, "errors": {"__all__": "Unexpected database error while saving. Please review values and try again."}, "field_meta": field_meta, "form_values": values}, status_code=500)

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
    field_meta = {c.name: build_field_meta(entity, c) for c in cols}
    return templates.TemplateResponse("entity_form.html", {"request": request, "user": user, "entity": entity, "cols": cols, "item": item, "errors": {}, "field_meta": field_meta, "form_values": {}})


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

