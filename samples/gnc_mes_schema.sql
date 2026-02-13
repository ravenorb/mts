
-- GNC Manufacturing Execution System (MES)
-- PostgreSQL Recommended

-- ===============================
-- ENGINEERING LAYER
-- ===============================

CREATE TABLE part (
    part_id SERIAL PRIMARY KEY,
    part_number TEXT UNIQUE NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    active BOOLEAN DEFAULT TRUE
);

CREATE TABLE part_revision (
    part_revision_id SERIAL PRIMARY KEY,
    part_id INT REFERENCES part(part_id),
    revision_code TEXT NOT NULL,
    is_current BOOLEAN DEFAULT FALSE,
    released_at TIMESTAMP,
    released_by TEXT,
    change_notes TEXT,
    UNIQUE(part_id, revision_code)
);

CREATE TABLE part_process_definition (
    process_id SERIAL PRIMARY KEY,
    part_revision_id INT REFERENCES part_revision(part_revision_id),

    laser_required BOOLEAN,
    laser_program_path TEXT,

    waterjet_required BOOLEAN,
    waterjet_program_path TEXT,

    forming_required BOOLEAN,
    forming_drawing_path TEXT,

    robotic_weld_required BOOLEAN,
    robotic_weld_program_path TEXT,

    manual_weld_required BOOLEAN,
    manual_weld_drawing_path TEXT
);

CREATE TABLE bill_of_material (
    bom_id SERIAL PRIMARY KEY,
    parent_part_revision_id INT REFERENCES part_revision(part_revision_id),
    component_part_revision_id INT REFERENCES part_revision(part_revision_id),
    quantity NUMERIC NOT NULL
);

-- ===============================
-- CUT SHEET SYSTEM
-- ===============================

CREATE TABLE cut_sheet (
    cut_sheet_id SERIAL PRIMARY KEY,
    cut_sheet_number TEXT,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    active BOOLEAN DEFAULT TRUE
);

CREATE TABLE cut_sheet_revision (
    cut_sheet_revision_id SERIAL PRIMARY KEY,
    cut_sheet_id INT REFERENCES cut_sheet(cut_sheet_id),
    revision_code TEXT,
    material_type TEXT,
    sheet_thickness TEXT,
    sheet_size TEXT,
    nc_file_path TEXT,
    pdf_path TEXT,
    nest_utilization_percent NUMERIC,
    released_at TIMESTAMP,
    released_by TEXT,
    is_current BOOLEAN DEFAULT FALSE,
    change_notes TEXT
);

CREATE TABLE cut_sheet_revision_output (
    output_id SERIAL PRIMARY KEY,
    cut_sheet_revision_id INT REFERENCES cut_sheet_revision(cut_sheet_revision_id),
    part_revision_id INT REFERENCES part_revision(part_revision_id),
    quantity_per_sheet NUMERIC,
    is_primary_part BOOLEAN
);

-- ===============================
-- PLANNING
-- ===============================

CREATE TABLE production_order (
    production_order_id SERIAL PRIMARY KEY,
    part_revision_id INT REFERENCES part_revision(part_revision_id),
    quantity_ordered NUMERIC,
    status TEXT,
    scheduled_start TIMESTAMP,
    scheduled_end TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ===============================
-- EXECUTION
-- ===============================

CREATE TABLE station (
    station_id SERIAL PRIMARY KEY,
    station_name TEXT,
    skill_required TEXT,
    active BOOLEAN DEFAULT TRUE
);

CREATE TABLE pallet (
    pallet_id SERIAL PRIMARY KEY,
    pallet_type TEXT,
    production_order_id INT REFERENCES production_order(production_order_id),
    cut_sheet_revision_id INT REFERENCES cut_sheet_revision(cut_sheet_revision_id),
    parent_pallet_id INT REFERENCES pallet(pallet_id),
    status TEXT,
    current_station_id INT REFERENCES station(station_id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT
);

CREATE TABLE pallet_part (
    pallet_part_id SERIAL PRIMARY KEY,
    pallet_id INT REFERENCES pallet(pallet_id),
    part_revision_id INT REFERENCES part_revision(part_revision_id),
    planned_quantity NUMERIC,
    actual_quantity NUMERIC,
    scrap_quantity NUMERIC
);

CREATE TABLE pallet_event (
    event_id SERIAL PRIMARY KEY,
    pallet_id INT REFERENCES pallet(pallet_id),
    station_id INT REFERENCES station(station_id),
    event_type TEXT,
    quantity NUMERIC,
    recorded_by TEXT,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

-- ===============================
-- LOCATION TRACKING
-- ===============================

CREATE TABLE location (
    location_id SERIAL PRIMARY KEY,
    location_name TEXT,
    location_type TEXT,
    parent_location_id INT REFERENCES location(location_id),
    active BOOLEAN DEFAULT TRUE
);

CREATE TABLE pallet_location_history (
    history_id SERIAL PRIMARY KEY,
    pallet_id INT REFERENCES pallet(pallet_id),
    from_location_id INT REFERENCES location(location_id),
    to_location_id INT REFERENCES location(location_id),
    moved_by TEXT,
    moved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reason TEXT
);

-- ===============================
-- SPLIT / MERGE
-- ===============================

CREATE TABLE pallet_split (
    split_id SERIAL PRIMARY KEY,
    source_pallet_id INT REFERENCES pallet(pallet_id),
    new_pallet_id INT REFERENCES pallet(pallet_id),
    performed_by TEXT,
    performed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reason TEXT
);

CREATE TABLE pallet_part_split (
    part_split_id SERIAL PRIMARY KEY,
    split_id INT REFERENCES pallet_split(split_id),
    part_revision_id INT REFERENCES part_revision(part_revision_id),
    quantity NUMERIC
);

CREATE TABLE pallet_merge (
    merge_id SERIAL PRIMARY KEY,
    target_pallet_id INT REFERENCES pallet(pallet_id),
    source_pallet_id INT REFERENCES pallet(pallet_id),
    merged_by TEXT,
    merged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reason TEXT
);

-- ===============================
-- MAINTENANCE
-- ===============================

CREATE TABLE station_maintenance_task (
    task_id SERIAL PRIMARY KEY,
    station_id INT REFERENCES station(station_id),
    task_description TEXT,
    frequency_hours NUMERIC,
    responsible_role TEXT,
    active BOOLEAN DEFAULT TRUE
);

CREATE TABLE station_maintenance_log (
    log_id SERIAL PRIMARY KEY,
    station_id INT REFERENCES station(station_id),
    task_id INT REFERENCES station_maintenance_task(task_id),
    performed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    performed_by TEXT,
    notes TEXT
);

-- ===============================
-- CONSUMABLES + PURCHASING
-- ===============================

CREATE TABLE consumable (
    consumable_id SERIAL PRIMARY KEY,
    description TEXT,
    vendor TEXT,
    vendor_part_number TEXT,
    unit_cost NUMERIC,
    qty_on_hand NUMERIC,
    qty_on_order NUMERIC,
    reorder_point NUMERIC
);

CREATE TABLE purchase_request (
    purchase_request_id SERIAL PRIMARY KEY,
    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    requested_by TEXT,
    status TEXT
);

CREATE TABLE purchase_request_line (
    line_id SERIAL PRIMARY KEY,
    purchase_request_id INT REFERENCES purchase_request(purchase_request_id),
    consumable_id INT REFERENCES consumable(consumable_id),
    quantity NUMERIC
);

CREATE TABLE consumable_usage_log (
    usage_id SERIAL PRIMARY KEY,
    consumable_id INT REFERENCES consumable(consumable_id),
    station_id INT REFERENCES station(station_id),
    quantity_delta NUMERIC,
    reason TEXT,
    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    purchase_request_id INT REFERENCES purchase_request(purchase_request_id)
);

-- ===============================
-- EMPLOYEE
-- ===============================

CREATE TABLE employee (
    employee_id SERIAL PRIMARY KEY,
    full_name TEXT,
    phone TEXT,
    email TEXT,
    hire_date DATE,
    active BOOLEAN DEFAULT TRUE
);

CREATE TABLE employee_skill (
    employee_skill_id SERIAL PRIMARY KEY,
    employee_id INT REFERENCES employee(employee_id),
    skill TEXT,
    achieved_at TIMESTAMP
);

-- ===============================
-- MATERIAL TRACEABILITY
-- ===============================

CREATE TABLE material_lot (
    lot_id SERIAL PRIMARY KEY,
    material_type TEXT,
    heat_number TEXT,
    vendor TEXT,
    received_date DATE
);

-- ===============================
-- JSON SNAPSHOT
-- ===============================

CREATE TABLE pallet_json_snapshot (
    snapshot_id SERIAL PRIMARY KEY,
    pallet_id INT REFERENCES pallet(pallet_id),
    snapshot_json JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
