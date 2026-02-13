from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="operator")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_code: Mapped[str] = mapped_column(String(40), unique=True)
    full_name: Mapped[str] = mapped_column(String(140))
    phone_number: Mapped[str] = mapped_column(String(40), default="")
    email_address: Mapped[str] = mapped_column(String(140), unique=True)
    start_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, unique=True)
    role: Mapped[str] = mapped_column(String(64), default="operator")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Skill(Base):
    __tablename__ = "skills"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")


class EmployeeSkill(Base):
    __tablename__ = "employee_skills"
    __table_args__ = (UniqueConstraint("employee_id", "skill_id", name="uq_employee_skill"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"))
    level: Mapped[int] = mapped_column(Integer, default=1)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Part(Base):
    __tablename__ = "parts"
    id: Mapped[int] = mapped_column(primary_key=True)
    part_number: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PartRevision(Base):
    __tablename__ = "part_revisions"
    __table_args__ = (UniqueConstraint("part_id", "revision_code", name="uq_part_rev"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"))
    revision_code: Mapped[str] = mapped_column(String(20))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    released_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    released_by: Mapped[str] = mapped_column(String(80), default="system")
    change_notes: Mapped[str] = mapped_column(Text, default="")


class PartRevisionFile(Base):
    __tablename__ = "part_revision_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    part_revision_id: Mapped[int] = mapped_column(ForeignKey("part_revisions.id"))
    file_type: Mapped[str] = mapped_column(String(40))
    original_name: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(Text)
    station_ids_csv: Mapped[str] = mapped_column(Text, default="")
    uploaded_by: Mapped[str] = mapped_column(String(80), default="system")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EngineeringQuestion(Base):
    __tablename__ = "engineering_questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    pallet_id: Mapped[int | None] = mapped_column(ForeignKey("pallets.id"), nullable=True)
    asked_by: Mapped[str] = mapped_column(String(80), default="operator")
    question_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PartProcessDefinition(Base):
    __tablename__ = "part_process_definitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    part_revision_id: Mapped[int] = mapped_column(ForeignKey("part_revisions.id"))
    laser_required: Mapped[bool] = mapped_column(Boolean, default=False)
    laser_program_path: Mapped[str] = mapped_column(Text, default="")
    waterjet_required: Mapped[bool] = mapped_column(Boolean, default=False)
    waterjet_program_path: Mapped[str] = mapped_column(Text, default="")
    forming_required: Mapped[bool] = mapped_column(Boolean, default=False)
    forming_drawing_path: Mapped[str] = mapped_column(Text, default="")
    robotic_weld_required: Mapped[bool] = mapped_column(Boolean, default=False)
    robotic_weld_program_path: Mapped[str] = mapped_column(Text, default="")
    manual_weld_required: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_weld_drawing_path: Mapped[str] = mapped_column(Text, default="")


class BillOfMaterial(Base):
    __tablename__ = "boms"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_part_revision_id: Mapped[int] = mapped_column(ForeignKey("part_revisions.id"))
    component_part_revision_id: Mapped[int] = mapped_column(ForeignKey("part_revisions.id"))
    quantity: Mapped[float] = mapped_column(Float)


class CutSheet(Base):
    __tablename__ = "cut_sheets"
    id: Mapped[int] = mapped_column(primary_key=True)
    cut_sheet_number: Mapped[str] = mapped_column(String(60), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CutSheetRevision(Base):
    __tablename__ = "cut_sheet_revisions"
    __table_args__ = (UniqueConstraint("cut_sheet_id", "revision_code", name="uq_cut_sheet_rev"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    cut_sheet_id: Mapped[int] = mapped_column(ForeignKey("cut_sheets.id"))
    revision_code: Mapped[str] = mapped_column(String(20))
    material_type: Mapped[str] = mapped_column(String(60), default="")
    sheet_thickness: Mapped[str] = mapped_column(String(40), default="")
    sheet_size: Mapped[str] = mapped_column(String(40), default="")
    nc_file_path: Mapped[str] = mapped_column(Text, default="")
    pdf_path: Mapped[str] = mapped_column(Text, default="")
    nest_utilization_percent: Mapped[float] = mapped_column(Float, default=0)
    released_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    released_by: Mapped[str] = mapped_column(String(80), default="system")
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    change_notes: Mapped[str] = mapped_column(Text, default="")


class CutSheetRevisionOutput(Base):
    __tablename__ = "cut_sheet_revision_outputs"
    id: Mapped[int] = mapped_column(primary_key=True)
    cut_sheet_revision_id: Mapped[int] = mapped_column(ForeignKey("cut_sheet_revisions.id"))
    part_revision_id: Mapped[int] = mapped_column(ForeignKey("part_revisions.id"))
    quantity_per_sheet: Mapped[float] = mapped_column(Float)
    is_primary_part: Mapped[bool] = mapped_column(Boolean, default=False)


class ProductionOrder(Base):
    __tablename__ = "production_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    part_revision_id: Mapped[int] = mapped_column(ForeignKey("part_revisions.id"))
    quantity_ordered: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(40), default="planned")
    scheduled_start: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    scheduled_end: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Station(Base):
    __tablename__ = "stations"
    id: Mapped[int] = mapped_column(primary_key=True)
    station_name: Mapped[str] = mapped_column(String(80), unique=True)
    skill_required: Mapped[str] = mapped_column(String(80), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Queue(Base):
    __tablename__ = "queues"
    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    pallet_id: Mapped[int] = mapped_column(ForeignKey("pallets.id"))
    queue_position: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), default="queued")


class Pallet(Base):
    __tablename__ = "pallets"
    id: Mapped[int] = mapped_column(primary_key=True)
    pallet_code: Mapped[str] = mapped_column(String(80), unique=True)
    pallet_type: Mapped[str] = mapped_column(String(40), default="manual")
    production_order_id: Mapped[int | None] = mapped_column(ForeignKey("production_orders.id"), nullable=True)
    cut_sheet_revision_id: Mapped[int | None] = mapped_column(ForeignKey("cut_sheet_revisions.id"), nullable=True)
    parent_pallet_id: Mapped[int | None] = mapped_column(ForeignKey("pallets.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="staged")
    current_station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[str] = mapped_column(String(80), default="system")


class PalletRevision(Base):
    __tablename__ = "pallet_revisions"
    __table_args__ = (UniqueConstraint("pallet_id", "revision_code", name="uq_pallet_rev"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    pallet_id: Mapped[int] = mapped_column(ForeignKey("pallets.id"))
    revision_code: Mapped[str] = mapped_column(String(20))
    snapshot_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[str] = mapped_column(String(80), default="system")


class PalletPart(Base):
    __tablename__ = "pallet_parts"
    id: Mapped[int] = mapped_column(primary_key=True)
    pallet_id: Mapped[int] = mapped_column(ForeignKey("pallets.id"))
    part_revision_id: Mapped[int] = mapped_column(ForeignKey("part_revisions.id"))
    planned_quantity: Mapped[float] = mapped_column(Float)
    actual_quantity: Mapped[float] = mapped_column(Float, default=0)
    scrap_quantity: Mapped[float] = mapped_column(Float, default=0)


class PalletEvent(Base):
    __tablename__ = "pallet_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    pallet_id: Mapped[int] = mapped_column(ForeignKey("pallets.id"))
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(40))
    quantity: Mapped[float] = mapped_column(Float, default=0)
    recorded_by: Mapped[str] = mapped_column(String(80), default="system")
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notes: Mapped[str] = mapped_column(Text, default="")


class StationMaintenanceTask(Base):
    __tablename__ = "station_maintenance_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    task_description: Mapped[str] = mapped_column(Text)
    frequency_hours: Mapped[float] = mapped_column(Float)
    responsible_role: Mapped[str] = mapped_column(String(80), default="maintenance")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class MaintenanceRequest(Base):
    __tablename__ = "maintenance_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    requested_by: Mapped[str] = mapped_column(String(80))
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    status: Mapped[str] = mapped_column(String(20), default="open")
    issue_description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Consumable(Base):
    __tablename__ = "consumables"
    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(String(140))
    vendor: Mapped[str] = mapped_column(String(120), default="")
    vendor_part_number: Mapped[str] = mapped_column(String(80), default="")
    unit_cost: Mapped[float] = mapped_column(Float, default=0)
    qty_on_hand: Mapped[float] = mapped_column(Float, default=0)
    qty_on_order: Mapped[float] = mapped_column(Float, default=0)
    qty_on_request: Mapped[float] = mapped_column(Float, default=0)
    reorder_point: Mapped[float] = mapped_column(Float, default=0)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("storage_locations.id"), nullable=True)


class PurchaseRequest(Base):
    __tablename__ = "purchase_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    requested_by: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="open")


class PurchaseRequestLine(Base):
    __tablename__ = "purchase_request_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_request_id: Mapped[int] = mapped_column(ForeignKey("purchase_requests.id"))
    consumable_id: Mapped[int] = mapped_column(ForeignKey("consumables.id"))
    quantity: Mapped[float] = mapped_column(Float)


class ConsumableUsageLog(Base):
    __tablename__ = "consumable_usage_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    consumable_id: Mapped[int] = mapped_column(ForeignKey("consumables.id"))
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    quantity_delta: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    logged_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    purchase_request_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_requests.id"), nullable=True)


class StorageLocation(Base):
    __tablename__ = "storage_locations"
    id: Mapped[int] = mapped_column(primary_key=True)
    location_description: Mapped[str] = mapped_column(String(200), default="")
    pallet_storage: Mapped[bool] = mapped_column(Boolean, default=False)
    shelf_count: Mapped[int] = mapped_column(Integer, default=1)
    bin_count: Mapped[int] = mapped_column(Integer, default=1)


class StorageBin(Base):
    __tablename__ = "storage_bins"
    __table_args__ = (UniqueConstraint("storage_location_id", "shelf_id", "bin_id", name="uq_storage_bin"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    storage_location_id: Mapped[int] = mapped_column(ForeignKey("storage_locations.id"))
    shelf_id: Mapped[int] = mapped_column(Integer)
    bin_id: Mapped[int] = mapped_column(Integer)
    qty: Mapped[float] = mapped_column(Float, default=0)
    pallet_id: Mapped[str] = mapped_column(String(80), default="")
    part_number: Mapped[str] = mapped_column(String(80), default="")
    description: Mapped[str] = mapped_column(String(200), default="")


class RawMaterial(Base):
    __tablename__ = "raw_materials"
    id: Mapped[int] = mapped_column(primary_key=True)
    gauge: Mapped[str] = mapped_column(String(40), default="")
    length: Mapped[float] = mapped_column(Float, default=0)
    width: Mapped[float] = mapped_column(Float, default=0)
    qty_on_hand: Mapped[float] = mapped_column(Float, default=0)
    qty_on_request: Mapped[float] = mapped_column(Float, default=0)
    qty_on_order: Mapped[float] = mapped_column(Float, default=0)
    storage_location_id: Mapped[int | None] = mapped_column(ForeignKey("storage_locations.id"), nullable=True)


class ScrapSteel(Base):
    __tablename__ = "scrap_steel"
    id: Mapped[int] = mapped_column(primary_key=True)
    pallet_id: Mapped[str] = mapped_column(String(80), default="")
    storage_id: Mapped[str] = mapped_column(String(80), default="")
    weight: Mapped[float] = mapped_column(Float, default=0)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("storage_locations.id"), nullable=True)
    scrap_type: Mapped[str] = mapped_column(String(80), default="")
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)


class PartInventory(Base):
    __tablename__ = "part_inventory"
    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"), unique=True)
    qty_on_hand_total: Mapped[float] = mapped_column(Float, default=0)
    qty_stored: Mapped[float] = mapped_column(Float, default=0)
    qty_queued_to_cut: Mapped[float] = mapped_column(Float, default=0)
    qty_to_bend: Mapped[float] = mapped_column(Float, default=0)
    qty_to_weld: Mapped[float] = mapped_column(Float, default=0)


class DeliveredPartLot(Base):
    __tablename__ = "delivered_part_lots"
    id: Mapped[int] = mapped_column(primary_key=True)
    frame_part_number: Mapped[str] = mapped_column(String(80))
    qty_completed_in_lot: Mapped[float] = mapped_column(Float, default=0)
    serial_begin: Mapped[str] = mapped_column(String(80), default="")
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    recorded_by: Mapped[str] = mapped_column(String(80), default="system")
