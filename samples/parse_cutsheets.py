#!/usr/bin/env python3
"""Parse cut sheet PDFs into simple JSON metadata attachments."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = ROOT / "samples"
OUTPUT_DIR = ROOT / "data" / "cutsheets"


@dataclass
class FileEntry:
    path: Path
    product: str
    station_code: str | None
    run_number: str | None


FILENAME_PATTERN = re.compile(
    r"^(?P<product>.+?)\s*-?\s*(?P<run>\d+)(?P<station>[A-Z]+)$"
)
PART_PATTERN = re.compile(r"\b(FR-[A-Z0-9]+)\b")
GAUGE_PATTERN = re.compile(r"\b(\d{1,2})\s*GA\b", re.IGNORECASE)
SHEET_SIZE_PATTERN = re.compile(
    r"(?P<width>\d+(?:\.\d+)?)'\s*[xX]\s*(?P<length>\d+(?:\.\d+)?)'"
)
DATE_PATTERN = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")
TIME_PATTERN = re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b")
NOTES_PATTERN = re.compile(
    r"\bmake(?:s)?\b|\bqty\b|\bquantity\b|\bframes?\b", re.IGNORECASE
)
HEADER_LABELS = {
    "#",
    "File Name",
    "Run Time",
    "x x",
    "Length Width Thickness",
    "# PcsPart #",
    "Date & Time",
    "Notes",
    "User data 3",
    "Weight Dimensions",
    "Material",
}


def parse_filename(path: Path) -> FileEntry:
    stem = path.stem
    match = FILENAME_PATTERN.match(stem)
    if match:
        product = match.group("product").strip()
        return FileEntry(
            path=path,
            product=product,
            station_code=match.group("station"),
            run_number=match.group("run"),
        )
    return FileEntry(path=path, product=stem, station_code=None, run_number=None)


def material_hint_from_product(product: str) -> str | None:
    match = re.search(r"([A-Z]{2,})$", product)
    return match.group(1) if match else None


def read_pdf_text(path: Path, *, layout: bool = False) -> str:
    reader = PdfReader(str(path))
    if layout:
        return "\n".join(
            page.extract_text(extraction_mode="layout") or "" for page in reader.pages
        )
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def parse_part_line(text: str) -> tuple[str, int | None] | None:
    if "FR-" not in text:
        return None
    match = re.search(r"(FR-[A-Z0-9]+)", text)
    if not match:
        return None
    part_number = match.group(1)
    quantity: int | None = None

    start_match = re.match(r"^(FR-[A-Z0-9]*[A-Z]+)(\d{1,2})\b", text)
    if start_match and re.search(r"\d+\.\d+", text):
        part_number = start_match.group(1)
        quantity = int(start_match.group(2))

    if quantity is None and part_number[-1].isalpha():
        appended_match = re.search(re.escape(part_number) + r"(\d{1,2})\b", text)
        if appended_match:
            quantity = int(appended_match.group(1))

    if quantity is None:
        integers = re.findall(r"(?<!\.)\b\d{1,2}\b(?!\.)", text)
        if integers:
            quantity = int(integers[-1])

    return part_number, quantity


def extract_parts_from_lines(lines: Iterable[str], source: str) -> list[dict]:
    parts: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        clean = " ".join(line.split())
        if clean.startswith("# Part #"):
            continue
        parsed = parse_part_line(clean)
        if not parsed:
            continue
        part_number, quantity = parsed
        parts.append(
            {
                "part_number": part_number,
                "quantity": quantity,
                "source": source,
            }
        )
    return parts


def collect_related_files(entries: list[FileEntry], current: FileEntry) -> list[dict]:
    related: list[dict] = []
    for entry in entries:
        if entry.path == current.path:
            continue
        if entry.product != current.product:
            continue
        relation = "same_product"
        if current.station_code and entry.station_code == current.station_code:
            relation = "same_station"
        related.append(
            {
                "path": str(entry.path.relative_to(ROOT)),
                "extension": entry.path.suffix.lower().lstrip("."),
                "station_code": entry.station_code,
                "relation": relation,
            }
        )
    return related


def parse_sheet_size(text: str) -> dict | None:
    match = SHEET_SIZE_PATTERN.search(text)
    if not match:
        return None
    width = float(match.group("width"))
    length = float(match.group("length"))
    if width > 20 or length > 30:
        return None
    return {"width": width, "length": length}


def parse_sheet_dimensions(text: str) -> dict | None:
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    if len(numbers) < 3:
        return None
    length, width, thickness = map(float, numbers[-3:])
    return {"length": length, "width": width, "thickness": thickness}


def parse_layout_metadata(lines: list[str], entry: FileEntry) -> dict:
    metadata: dict[str, str | dict | None] = {
        "file_name": None,
        "run_time": None,
        "date_time": None,
        "user_data_3": None,
        "notes": None,
        "material_type": None,
        "gauge": None,
        "sheet_size_ft": None,
        "sheet_dimensions_in": None,
        "company_name": None,
        "machine_type": None,
        "software_used": None,
    }

    for line in lines:
        normalized = " ".join(line.split())
        if not normalized:
            continue

        if normalized.startswith("File Name"):
            remainder = normalized[len("File Name") :].strip()
            if remainder:
                metadata["file_name"] = remainder
                gauge_match = GAUGE_PATTERN.search(remainder)
                metadata["gauge"] = metadata["gauge"] or (
                    gauge_match.group(1) if gauge_match else None
                )
                metadata["sheet_size_ft"] = metadata["sheet_size_ft"] or parse_sheet_size(
                    remainder
                )
        elif normalized.startswith("Run Time"):
            match = TIME_PATTERN.search(normalized)
            if match:
                metadata["run_time"] = match.group(0)
        elif normalized.startswith("Date & Time"):
            date_match = DATE_PATTERN.search(normalized)
            time_match = TIME_PATTERN.search(normalized)
            if date_match and time_match:
                metadata["date_time"] = f"{date_match.group(0)} {time_match.group(0)}"
            elif date_match:
                metadata["date_time"] = date_match.group(0)
            dimensions = parse_sheet_dimensions(normalized)
            if dimensions:
                metadata["sheet_dimensions_in"] = dimensions
        elif normalized.startswith("User data 3"):
            remainder = normalized[len("User data 3") :].strip()
            if "Material" in remainder:
                parts = remainder.split("Material", 1)
                metadata["user_data_3"] = parts[0].strip() or metadata["user_data_3"]
                metadata["material_type"] = parts[1].strip() or metadata["material_type"]
            else:
                metadata["user_data_3"] = remainder or metadata["user_data_3"]
        elif normalized.startswith("Notes"):
            remainder = normalized[len("Notes") :].strip()
            metadata["notes"] = remainder or metadata["notes"]
        elif "Company" in normalized and not metadata["company_name"]:
            metadata["company_name"] = normalized.split("Company", 1)[-1].strip() or None
        elif "Machine" in normalized and not metadata["machine_type"]:
            metadata["machine_type"] = normalized.split("Machine", 1)[-1].strip() or None
        elif "Software" in normalized and not metadata["software_used"]:
            metadata["software_used"] = normalized.split("Software", 1)[-1].strip() or None

    if metadata["file_name"]:
        gauge_match = GAUGE_PATTERN.search(str(metadata["file_name"]))
        if gauge_match and not metadata["gauge"]:
            metadata["gauge"] = gauge_match.group(1)
        if not metadata["sheet_size_ft"]:
            metadata["sheet_size_ft"] = parse_sheet_size(str(metadata["file_name"]))
        name_value = str(metadata["file_name"])
        if metadata["gauge"]:
            name_value = GAUGE_PATTERN.split(name_value, maxsplit=1)[0].strip()
        if name_value:
            metadata["file_name"] = name_value
    else:
        metadata["file_name"] = entry.path.stem

    return metadata


def parse_plain_metadata(lines: list[str]) -> dict:
    filtered = []
    for line in lines:
        if line in HEADER_LABELS:
            continue
        if line.startswith("DWG#"):
            break
        filtered.append(line)
        if PART_PATTERN.search(line):
            break

    candidates = [line for line in filtered if line and not PART_PATTERN.search(line)]
    date_time = next((line for line in candidates if DATE_PATTERN.search(line)), None)
    run_time = next(
        (line for line in candidates if TIME_PATTERN.search(line) and line != date_time),
        None,
    )
    material_line = next(
        (line for line in candidates if GAUGE_PATTERN.search(line) or SHEET_SIZE_PATTERN.search(line)),
        None,
    )
    dimensions_line = next(
        (line for line in candidates if parse_sheet_dimensions(line)), None
    )
    description_lines = [
        line
        for line in candidates
        if line not in {date_time, run_time, material_line, dimensions_line}
    ]

    return {
        "date_time": date_time,
        "run_time": run_time,
        "material_line": material_line,
        "sheet_dimensions_in": parse_sheet_dimensions(dimensions_line or ""),
        "description_lines": description_lines,
    }


def notes_score(text: str | None) -> int:
    if not text:
        return 0
    score = 0
    if re.search(r"\bmake(?:s)?\b|\bqty\b|\bquantity\b", text, re.IGNORECASE):
        score += 2
    if re.search(r"\bframes?\b", text, re.IGNORECASE):
        score += 1
    if re.search(r"\d+", text):
        score += 1
    return score


def resolve_notes_and_description(
    notes: str | None, description: str | None
) -> tuple[str | None, str | None]:
    if notes and description:
        if notes_score(description) > notes_score(notes):
            return description, notes
    return notes, description


def slice_part_section(lines: list[str], header_hint: str | None = None) -> list[str]:
    start_index = 0
    if header_hint:
        for i, line in enumerate(lines):
            if header_hint in line:
                start_index = i + 1
                break
    else:
        for i, line in enumerate(lines):
            if PART_PATTERN.search(line):
                start_index = i
                break
    end_index = len(lines)
    for i in range(start_index, len(lines)):
        if any(token in lines[i] for token in ("DWG#", "DATE PRINTED", "DESCRIPTION")):
            end_index = i
            break
    return lines[start_index:end_index]


def extract_metadata(entry: FileEntry, all_entries: list[FileEntry]) -> dict:
    text = read_pdf_text(entry.path)
    layout_text = read_pdf_text(entry.path, layout=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    layout_lines = [line.strip() for line in layout_text.splitlines() if line.strip()]

    layout_metadata = parse_layout_metadata(layout_lines, entry)
    plain_metadata = parse_plain_metadata(lines)

    if not layout_metadata.get("gauge") and plain_metadata.get("material_line"):
        gauge_match = GAUGE_PATTERN.search(plain_metadata["material_line"])
        if gauge_match:
            layout_metadata["gauge"] = gauge_match.group(1)

    if not layout_metadata.get("sheet_size_ft") and plain_metadata.get("material_line"):
        layout_metadata["sheet_size_ft"] = parse_sheet_size(plain_metadata["material_line"])

    if not layout_metadata.get("sheet_dimensions_in"):
        layout_metadata["sheet_dimensions_in"] = plain_metadata.get("sheet_dimensions_in")

    if not layout_metadata.get("run_time"):
        layout_metadata["run_time"] = plain_metadata.get("run_time")
    if not layout_metadata.get("date_time"):
        layout_metadata["date_time"] = plain_metadata.get("date_time")

    description_lines = plain_metadata.get("description_lines", [])
    if not layout_metadata.get("notes") and description_lines:
        layout_metadata["notes"] = description_lines[0]
    if not layout_metadata.get("user_data_3") and len(description_lines) > 1:
        layout_metadata["user_data_3"] = description_lines[1]

    notes, description = resolve_notes_and_description(
        layout_metadata.get("notes"), layout_metadata.get("user_data_3")
    )
    layout_metadata["notes"] = notes
    layout_metadata["user_data_3"] = description

    if not layout_metadata.get("material_type"):
        layout_metadata["material_type"] = material_hint_from_product(entry.product)

    layout_part_lines = slice_part_section(layout_lines, header_hint="# Part #")
    plain_part_lines = slice_part_section(lines)

    parts = extract_parts_from_lines(layout_part_lines, "layout")
    if not parts:
        parts = extract_parts_from_lines(plain_part_lines, "line_scan")

    deduped_parts: dict[str, dict] = {}
    for part in parts:
        existing = deduped_parts.get(part["part_number"])
        if existing is None or (existing["quantity"] is None and part["quantity"] is not None):
            deduped_parts[part["part_number"]] = {
                "part_number": part["part_number"],
                "quantity": part["quantity"],
            }

    return {
        "cutsheet": {
            "file_name": entry.path.name,
            "source_pdf": str(entry.path.relative_to(ROOT)),
            "product": entry.product,
            "station_code": entry.station_code,
            "run_number": entry.run_number,
            "material_hint": material_hint_from_product(entry.product),
            "file_type": entry.path.suffix.lower().lstrip("."),
        },
        "parsed_from_pdf": {
            "company_name": layout_metadata.get("company_name"),
            "machine_type": layout_metadata.get("machine_type"),
            "software_used": layout_metadata.get("software_used"),
            "file_name": layout_metadata.get("file_name"),
            "run_time": layout_metadata.get("run_time"),
            "date_time": layout_metadata.get("date_time"),
            "user_data_3": layout_metadata.get("user_data_3"),
            "notes": layout_metadata.get("notes"),
            "material_type": layout_metadata.get("material_type"),
            "gauge": layout_metadata.get("gauge"),
            "sheet_size_ft": layout_metadata.get("sheet_size_ft"),
            "sheet_dimensions_in": layout_metadata.get("sheet_dimensions_in"),
        },
        "parts": list(deduped_parts.values()),
        "related_files": collect_related_files(all_entries, entry),
    }


def write_metadata() -> list[dict]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(SAMPLES_DIR.glob("*.pdf"))
    entries = [parse_filename(pdf) for pdf in pdfs + list(SAMPLES_DIR.glob("*.MPF"))]
    pdf_entries = [entry for entry in entries if entry.path.suffix.lower() == ".pdf"]

    index: list[dict] = []
    for entry in pdf_entries:
        metadata = extract_metadata(entry, entries)
        output_path = OUTPUT_DIR / f"{entry.path.stem}.json"
        output_path.write_text(json.dumps(metadata, indent=2, sort_keys=True))
        index.append(
            {
                "product": entry.product,
                "station_code": entry.station_code,
                "cutsheet_metadata": str(output_path.relative_to(ROOT)),
            }
        )
    return index


def main() -> None:
    index = write_metadata()
    index_path = OUTPUT_DIR / "index.json"
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
