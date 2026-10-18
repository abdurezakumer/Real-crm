import csv
import uuid
from pathlib import Path

import pytest
from openpyxl import Workbook

from realestate_crm.phone import normalize_phone
from realestate_crm.services.importing import (
    ColumnMappingError,
    preview_file,
    process_import_batch,
)


class Cursor:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


class FakeImportConnection:
    def __init__(self, existing=None):
        self.contacts = existing or {}
        self.campaign_links = set()
        self.last_update = None
        self.last_insert_sql = None
        self.campaign_source = None

    def execute(self, sql, params=()):
        if sql.startswith("select normalized_phone from contacts"):
            return Cursor(
                rows=[{"normalized_phone": phone} for phone in params[0] if phone in self.contacts]
            )
        if sql.startswith("select source from campaigns"):
            return Cursor(row={"source": self.campaign_source})
        if sql.startswith("select id, normalized_phone from contacts"):
            return Cursor(
                rows=[
                    {"id": row["id"], "normalized_phone": phone}
                    for phone, row in self.contacts.items()
                    if phone in params[0]
                ]
            )
        if sql.startswith("update import_jobs"):
            self.last_update = (sql, params)
            return Cursor()
        if sql.startswith("insert into lead_assignment_history"):
            return Cursor()
        raise AssertionError(f"Unexpected SQL: {sql[:80]}")

    def executemany(self, sql, rows):
        rows = list(rows)
        if sql.startswith("insert into contacts"):
            self.last_insert_sql = sql
            is_update = "do update set name" in sql
            for (
                name,
                phone_number,
                normalized_phone,
                source_row,
                assigned_user,
                batch_id,
                source,
            ) in rows:
                if normalized_phone in self.contacts:
                    if is_update:
                        self.contacts[normalized_phone]["name"] = name
                        self.contacts[normalized_phone]["phone_number"] = phone_number
                        self.contacts[normalized_phone]["source_row_number"] = source_row
                        self.contacts[normalized_phone]["import_batch_id"] = batch_id
                        self.contacts[normalized_phone]["lead_source"] = source
                    continue
                self.contacts[normalized_phone] = {
                    "id": uuid.uuid4(),
                    "name": name,
                    "phone_number": phone_number,
                    "source_row_number": source_row,
                    "assigned_telegram_user_id": assigned_user,
                    "import_batch_id": batch_id,
                    "lead_source": source,
                    "lead_status": "NEW",
                    "call_history": [],
                }
        elif sql.startswith("insert into campaign_contacts"):
            self.campaign_links.update(rows)
        else:
            raise AssertionError(f"Unexpected executemany SQL: {sql[:80]}")


def make_xlsx(path: Path, rows: list[tuple], headers=("No.", "Name", "Phone Number")) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return path


def make_job(filename: str, campaign_id=None, policy="ADD_TO_CAMPAIGN"):
    return {
        "id": uuid.uuid4(),
        "telegram_user_id": 77,
        "original_filename": filename,
        "header_mapping": {"source_row_number": 0, "name": 1, "phone": 2},
        "next_row_number": 2,
        "error_summary": [],
        "import_policy": policy,
        "campaign_id": campaign_id,
        "total_rows": None,
        "imported_rows": 0,
        "skipped_rows": 0,
        "duplicate_rows": 0,
        "invalid_rows": 0,
    }


def test_normalizes_et_phone_and_rejects_invalid():
    assert normalize_phone("0911234567") == "+251911234567"
    assert normalize_phone("+251 911 234 567") == "+251911234567"
    assert normalize_phone("12345") is None


def test_xlsx_preview_recognizes_common_headers(tmp_path):
    path = make_xlsx(tmp_path / "leads.xlsx", [(1, "Alem", "0911234567")])
    preview = preview_file(path, "leads.xlsx")
    assert preview.mapping == {"source_row_number": 0, "name": 1, "phone": 2}
    assert preview.rows == [{"name": "Alem", "phone": "0911234567"}]


def test_csv_preview_and_missing_columns(tmp_path):
    path = tmp_path / "leads.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Full Name", "Mobile"])
        writer.writerow(["Liya", "0911234567"])
    assert preview_file(path, path.name).rows[0]["name"] == "Liya"
    with path.open("w", encoding="utf-8", newline="") as file:
        csv.writer(file).writerow(["Company", "City"])
    with pytest.raises(ColumnMappingError):
        preview_file(path, path.name)


def test_ambiguous_columns_can_be_selected(tmp_path):
    path = make_xlsx(tmp_path / "ambiguous.xlsx", [], headers=("Name", "Name", "Phone"))
    with pytest.raises(ColumnMappingError) as error:
        preview_file(path, path.name)
    assert error.value.candidates["name"] == [0, 1]
    selected = preview_file(path, path.name, {"name": 1, "phone": 2})
    assert selected.mapping == {"name": 1, "phone": 2}


def test_import_reports_duplicates_and_invalid_rows(tmp_path):
    campaign_id = uuid.uuid4()
    path = make_xlsx(
        tmp_path / "leads.xlsx",
        [
            (1, "Alem", "0911234567"),
            (2, "Duplicate", "+251911234567"),
            (3, "Bad number", "12345"),
            (4, "", ""),
        ],
    )
    conn = FakeImportConnection()
    conn.campaign_source = "Telegram leads"
    job = make_job(path.name, campaign_id)
    result = process_import_batch(conn, job, path, 20, 1000)
    phone = "+251911234567"
    assert result["status"] == "COMPLETED"
    assert result["imported"] == 1
    assert result["duplicates"] == 1
    assert result["invalid"] == 2
    assert set(conn.contacts) == {phone}
    assert conn.contacts[phone]["import_batch_id"] == job["id"]
    assert conn.contacts[phone]["lead_source"] == "Telegram leads"
    assert len(conn.campaign_links) == 1
    errors = conn.last_update[1][-3]
    assert "Duplicate phone number" in errors or "Invalid or missing" in errors


def test_reimport_skip_existing_and_add_to_second_campaign_preserves_history(tmp_path):
    phone = "+251911234567"
    existing_id = uuid.uuid4()
    existing = {
        phone: {
            "id": existing_id,
            "name": "Old Name",
            "phone_number": phone,
            "source_row_number": 7,
            "lead_status": "INTERESTED",
            "call_history": ["call-1"],
        }
    }
    path = make_xlsx(tmp_path / "repeat.xlsx", [(1, "New Name", "0911234567")])
    campaign_two = uuid.uuid4()

    add_conn = FakeImportConnection(existing)
    add_result = process_import_batch(add_conn, make_job(path.name, campaign_two), path, 20, 1000)
    assert add_result["duplicates"] == 1
    assert add_result["imported"] == 1
    assert (campaign_two, existing_id) in add_conn.campaign_links
    assert add_conn.contacts[phone]["call_history"] == ["call-1"]
    assert add_conn.contacts[phone]["lead_status"] == "INTERESTED"

    skip_conn = FakeImportConnection(existing)
    skip_result = process_import_batch(
        skip_conn, make_job(path.name, uuid.uuid4(), "SKIP_EXISTING"), path, 20, 1000
    )
    assert skip_result["skipped"] == 1
    assert skip_result["imported"] == 0
    assert skip_conn.contacts[phone]["call_history"] == ["call-1"]


def test_update_existing_changes_contact_fields_only(tmp_path):
    phone = "+251911234567"
    existing_id = uuid.uuid4()
    conn = FakeImportConnection(
        {
            phone: {
                "id": existing_id,
                "name": "Old Name",
                "phone_number": phone,
                "source_row_number": 7,
                "lead_status": "INTERESTED",
                "call_history": ["history"],
            }
        }
    )
    path = make_xlsx(tmp_path / "update.xlsx", [(1, "Updated Name", "0911234567")])
    result = process_import_batch(
        conn, make_job(path.name, None, "UPDATE_EXISTING"), path, 20, 1000
    )
    assert result["imported"] == 1
    assert conn.contacts[phone]["name"] == "Updated Name"
    assert conn.contacts[phone]["lead_status"] == "INTERESTED"
    assert conn.contacts[phone]["call_history"] == ["history"]
    assert "lead_status" not in conn.last_insert_sql
    assert "call" not in conn.last_insert_sql
