"""Parse lead rows pasted into Telegram messages."""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from realestate_crm.phone import normalize_phone
from realestate_crm.services.importing import ALIASES, normalized_header

_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+\s*2\s*5\s*1|2\s*5\s*1|0)?\s*9"
    r"(?:[\s().-]*\d){8}(?!\d)"
)
_DELIMITERS = ("\t", ",", ";", "|")


def _header_mapping(headers: list[str]) -> dict[str, int] | None:
    found = {
        key: [index for index, header in enumerate(headers) if normalized_header(header) in aliases]
        for key, aliases in ALIASES.items()
    }
    if len(found["name"]) == 1 and len(found["phone"]) == 1:
        mapping = {key: values[0] for key, values in found.items() if len(values) == 1}
        return mapping
    return None


def _delimiter(text: str) -> str | None:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    counts = {
        delimiter: sum(line.count(delimiter) for line in lines[:30]) for delimiter in _DELIMITERS
    }
    chosen = max(counts, key=counts.get)
    if counts[chosen] and sum(chosen in line for line in lines[:30]) >= min(2, len(lines)):
        return chosen
    return None


def _clean_cell(value: Any) -> str:
    return str(value or "").strip()


def _phone_cell(value: str) -> tuple[str | None, str | None]:
    normalized = normalize_phone(value)
    if normalized:
        return normalized, value.strip()
    for match in _PHONE_PATTERN.finditer(value):
        normalized = normalize_phone(match.group())
        if normalized:
            return normalized, match.group().strip()
    return None, None


def _phone_span(line: str) -> tuple[re.Match[str], str] | None:
    for match in _PHONE_PATTERN.finditer(line):
        normalized = normalize_phone(match.group())
        if normalized:
            return match, normalized
    return None


def _split_prefix(prefix: str) -> tuple[str, str]:
    prefix = prefix.strip(" \t,;|:-")
    match = re.match(r"^(\d{1,9})[.)\s:-]+(.+)$", prefix)
    if match:
        return match.group(1), match.group(2).strip()
    return "", prefix


def _delimited_record(cells: list[str]) -> dict[str, str] | None:
    phone_index = next((index for index, cell in enumerate(cells) if _phone_cell(cell)[0]), None)
    if phone_index is None:
        # Keep rows with an apparent name so the normal import worker can count
        # invalid or missing phone numbers in its final summary.
        name = " ".join(cell for cell in cells if cell).strip()
        return {"source_row_number": "", "name": name, "phone": ""} if name else None

    normalized, phone_raw = _phone_cell(cells[phone_index])
    remaining = [cell for index, cell in enumerate(cells) if index != phone_index and cell]
    source_number = ""
    if remaining and re.fullmatch(r"\d{1,9}", remaining[0]):
        source_number = remaining.pop(0)
    return {
        "source_row_number": source_number,
        "name": " ".join(remaining).strip(),
        "phone": phone_raw or normalized or "",
    }


def _header_record(cells: list[str], mapping: dict[str, int]) -> dict[str, str] | None:
    if not any(cell.strip() for cell in cells):
        return None
    name_index, phone_index = mapping["name"], mapping["phone"]
    name = cells[name_index].strip() if name_index < len(cells) else ""
    phone = cells[phone_index].strip() if phone_index < len(cells) else ""
    no_index = mapping.get("source_row_number")
    source_number = (
        cells[no_index].strip() if no_index is not None and no_index < len(cells) else ""
    )
    return {"source_row_number": source_number, "name": name, "phone": phone}


def _space_header(text: str) -> list[str] | None:
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    columns = [part.strip() for part in re.split(r"\s{2,}", first_line) if part.strip()]
    if len(columns) < 2:
        columns = first_line.split()
    return columns if _header_mapping(columns) else None


def parse_pasted_leads(text: str, max_rows: int) -> tuple[list[dict[str, str]], int]:
    """Return normalized lead records and the number of unrecognized lines."""
    cleaned = text.lstrip("\ufeff").strip()
    if not cleaned:
        raise ValueError("Paste one lead per line, with a name and Ethiopian phone number.")

    delimiter = _delimiter(cleaned)
    lines = [line for line in cleaned.splitlines() if line.strip()]
    records: list[dict[str, str]] = []
    skipped = 0

    if delimiter:
        rows = list(csv.reader(io.StringIO(cleaned), delimiter=delimiter))
        headers = [_clean_cell(cell) for cell in rows[0]] if rows else []
        mapping = _header_mapping(headers)
        data_rows = rows[1:] if mapping else rows
        for row in data_rows:
            cells = [_clean_cell(cell) for cell in row]
            record = _header_record(cells, mapping) if mapping else _delimited_record(cells)
            if record:
                records.append(record)
            else:
                skipped += 1
    else:
        headers = _space_header(cleaned)
        mapping = _header_mapping(headers) if headers else None
        data_lines = lines[1:] if mapping else lines
        for line in data_lines:
            found = _phone_span(line)
            if not found:
                if mapping:
                    skipped += 1
                else:
                    skipped += 1
                continue
            match, normalized = found
            source_number, name = _split_prefix(line[: match.start()])
            suffix = line[match.end() :].strip(" \t,;|:-")
            if suffix:
                name = f"{name} {suffix}".strip()
            records.append(
                {
                    "source_row_number": source_number,
                    "name": name,
                    "phone": match.group().strip() or normalized,
                }
            )

    if len(records) > max_rows:
        raise ValueError(f"This paste contains more than the {max_rows:,} row import limit.")
    if not records:
        raise ValueError(
            "I couldn't find lead rows. Send one lead per line or paste a table with Name and Phone columns."
        )
    return records, skipped
