-- Manufacturing Tracking System schema (SQLite/PostgreSQL-friendly)
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL,
  active BOOLEAN DEFAULT 1
);
CREATE TABLE IF NOT EXISTS employees (
  id INTEGER PRIMARY KEY,
  employee_code TEXT UNIQUE,
  full_name TEXT,
  phone_number TEXT,
  email_address TEXT UNIQUE,
  username TEXT UNIQUE,
  password_hash TEXT,
  start_date TIMESTAMP,
  user_id INTEGER UNIQUE REFERENCES users(id),
  role TEXT,
  active BOOLEAN DEFAULT 1
);
CREATE TABLE IF NOT EXISTS skills (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
CREATE TABLE IF NOT EXISTS employee_skills (
  id INTEGER PRIMARY KEY,
  employee_id INTEGER REFERENCES employees(id),
  skill_id INTEGER REFERENCES skills(id),
  level INTEGER DEFAULT 1,
  acquired_at TIMESTAMP,
  UNIQUE(employee_id, skill_id)
);
CREATE TABLE IF NOT EXISTS parts (id INTEGER PRIMARY KEY, part_number TEXT UNIQUE, description TEXT, created_at TIMESTAMP, active BOOLEAN DEFAULT 1);
CREATE TABLE IF NOT EXISTS part_revisions (
  id INTEGER PRIMARY KEY,
  part_id INTEGER REFERENCES parts(id),
  revision_code TEXT,
  is_current BOOLEAN DEFAULT 0,
  released_at TIMESTAMP,
  released_by TEXT,
  change_notes TEXT,
  UNIQUE(part_id, revision_code)
);
CREATE TABLE IF NOT EXISTS part_process_definitions (
  id INTEGER PRIMARY KEY,
  part_revision_id INTEGER REFERENCES part_revisions(id),
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
CREATE TABLE IF NOT EXISTS boms (id INTEGER PRIMARY KEY, parent_part_revision_id INTEGER REFERENCES part_revisions(id), component_part_revision_id INTEGER REFERENCES part_revisions(id), quantity REAL);
CREATE TABLE IF NOT EXISTS cut_sheets (id INTEGER PRIMARY KEY, cut_sheet_number TEXT UNIQUE, description TEXT, created_at TIMESTAMP, active BOOLEAN DEFAULT 1);
CREATE TABLE IF NOT EXISTS cut_sheet_revisions (
  id INTEGER PRIMARY KEY,
  cut_sheet_id INTEGER REFERENCES cut_sheets(id),
  revision_code TEXT,
  material_type TEXT,
  sheet_thickness TEXT,
  sheet_size TEXT,
  nc_file_path TEXT,
  pdf_path TEXT,
  nest_utilization_percent REAL,
  released_at TIMESTAMP,
  released_by TEXT,
  is_current BOOLEAN DEFAULT 0,
  change_notes TEXT,
  UNIQUE(cut_sheet_id, revision_code)
);
CREATE TABLE IF NOT EXISTS cut_sheet_revision_outputs (id INTEGER PRIMARY KEY, cut_sheet_revision_id INTEGER REFERENCES cut_sheet_revisions(id), part_revision_id INTEGER REFERENCES part_revisions(id), quantity_per_sheet REAL, is_primary_part BOOLEAN);
CREATE TABLE IF NOT EXISTS production_orders (id INTEGER PRIMARY KEY, part_revision_id INTEGER REFERENCES part_revisions(id), quantity_ordered REAL, status TEXT, scheduled_start TIMESTAMP, scheduled_end TIMESTAMP, created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS stations (id INTEGER PRIMARY KEY, station_name TEXT UNIQUE, skill_required TEXT, active BOOLEAN DEFAULT 1);
CREATE TABLE IF NOT EXISTS pallets (
  id INTEGER PRIMARY KEY,
  pallet_code TEXT UNIQUE,
  pallet_type TEXT,
  production_order_id INTEGER REFERENCES production_orders(id),
  cut_sheet_revision_id INTEGER REFERENCES cut_sheet_revisions(id),
  parent_pallet_id INTEGER REFERENCES pallets(id),
  status TEXT,
  current_station_id INTEGER REFERENCES stations(id),
  created_at TIMESTAMP,
  created_by TEXT
);
CREATE TABLE IF NOT EXISTS pallet_revisions (id INTEGER PRIMARY KEY, pallet_id INTEGER REFERENCES pallets(id), revision_code TEXT, snapshot_json TEXT, created_at TIMESTAMP, created_by TEXT, UNIQUE(pallet_id, revision_code));
CREATE TABLE IF NOT EXISTS pallet_parts (id INTEGER PRIMARY KEY, pallet_id INTEGER REFERENCES pallets(id), part_revision_id INTEGER REFERENCES part_revisions(id), planned_quantity REAL, actual_quantity REAL, scrap_quantity REAL);
CREATE TABLE IF NOT EXISTS pallet_events (id INTEGER PRIMARY KEY, pallet_id INTEGER REFERENCES pallets(id), station_id INTEGER REFERENCES stations(id), event_type TEXT, quantity REAL, recorded_by TEXT, recorded_at TIMESTAMP, notes TEXT);
CREATE TABLE IF NOT EXISTS queues (id INTEGER PRIMARY KEY, station_id INTEGER REFERENCES stations(id), pallet_id INTEGER REFERENCES pallets(id), queue_position INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS station_maintenance_tasks (id INTEGER PRIMARY KEY, station_id INTEGER REFERENCES stations(id), task_description TEXT, frequency_hours REAL, responsible_role TEXT, active BOOLEAN DEFAULT 1);
CREATE TABLE IF NOT EXISTS maintenance_requests (id INTEGER PRIMARY KEY, station_id INTEGER REFERENCES stations(id), requested_by TEXT, priority TEXT, status TEXT, issue_description TEXT, created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS consumables (id INTEGER PRIMARY KEY, description TEXT, vendor TEXT, vendor_part_number TEXT, unit_cost REAL, qty_on_hand REAL, qty_on_order REAL, reorder_point REAL);
CREATE TABLE IF NOT EXISTS purchase_requests (id INTEGER PRIMARY KEY, requested_at TIMESTAMP, requested_by TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS purchase_request_lines (id INTEGER PRIMARY KEY, purchase_request_id INTEGER REFERENCES purchase_requests(id), consumable_id INTEGER REFERENCES consumables(id), quantity REAL);
CREATE TABLE IF NOT EXISTS consumable_usage_logs (id INTEGER PRIMARY KEY, consumable_id INTEGER REFERENCES consumables(id), station_id INTEGER REFERENCES stations(id), quantity_delta REAL, reason TEXT, logged_at TIMESTAMP, purchase_request_id INTEGER REFERENCES purchase_requests(id));
