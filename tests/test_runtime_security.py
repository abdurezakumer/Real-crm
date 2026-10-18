from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from telegram.error import TelegramError

from realestate_crm import runtime, timeutils
from realestate_crm import worker as worker_module


def settings():
    return SimpleNamespace(
        telegram_webhook_secret=SecretStr("webhook-secret"),
        follow_up_cron_secret=SecretStr("cron-secret"),
        authorized_telegram_user_id=77,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [None, "", "wrong-secret"])
async def test_webhook_rejects_missing_or_wrong_secret_before_processing(monkeypatch, secret):
    monkeypatch.setattr(runtime, "get_settings", settings)
    status, response = await runtime.process_webhook({"update_id": 1}, secret)
    assert status == 403
    assert response == {"ok": False, "error": "unauthorized"}


@pytest.mark.asyncio
async def test_webhook_ignores_non_authorized_numeric_user(monkeypatch):
    monkeypatch.setattr(runtime, "get_settings", settings)
    payload = {
        "update_id": 2,
        "message": {"from": {"id": 88, "username": "authorized_by_name_only"}},
    }
    status, response = await runtime.process_webhook(payload, "webhook-secret")
    assert status == 200
    assert response == {"ok": True}


@pytest.mark.asyncio
async def test_webhook_ignores_authorized_user_in_group_chat(monkeypatch):
    monkeypatch.setattr(runtime, "get_settings", settings)
    payload = {
        "update_id": 3,
        "message": {
            "from": {"id": 77},
            "chat": {"id": -100123, "type": "supergroup"},
            "text": "/start",
        },
    }
    status, response = await runtime.process_webhook(payload, "webhook-secret")
    assert status == 200
    assert response == {"ok": True}


@pytest.mark.asyncio
async def test_worker_requires_bearer_secret(monkeypatch):
    monkeypatch.setattr(runtime, "get_settings", settings)
    status, response = await runtime.process_worker("Bearer wrong-secret")
    assert status == 403
    assert response == {"ok": False, "error": "unauthorized"}


def test_addis_ababa_day_bounds_are_utc_and_half_open():
    now = datetime(2026, 10, 16, 7, 30, tzinfo=UTC)
    start, end = timeutils.local_day_bounds(now, "Africa/Addis_Ababa")
    assert start == datetime(2026, 10, 15, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 16, 21, 0, tzinfo=UTC)
    assert start <= now < end


def test_export_escapes_spreadsheet_formula_prefixes():
    row = runtime._export_row(
        {
            "id": "contact-id",
            "name": '=HYPERLINK("https://bad.example")',
            "phone_number": "+251911234567",
            "lead_status": "NEW",
            "interest_level": "UNKNOWN",
            "total_call_attempts": 0,
            "last_called_at": None,
            "next_follow_up_at": None,
            "contact_eligibility": "ELIGIBLE",
        },
        "Africa/Addis_Ababa",
    )
    assert row[1].startswith("'=")
    assert row[2] == "'+251911234567"


class DashboardConnection:
    def __init__(self):
        self.queries = []

    def execute(self, sql, params=()):
        self.queries.append((" ".join(sql.lower().split()), params))
        if "insert into user_settings" in sql.lower():
            return RowCursor(
                {
                    "timezone": "Africa/Addis_Ababa",
                    "daily_call_target": 40,
                }
            )
        return RowCursor(
            {
                "total_contacts": 120,
                "eligible_contacts": 90,
                "never_called": 55,
                "calls_today": 10,
                "answered_today": 6,
                "interested_today": 3,
                "due_today": 4,
                "overdue": 2,
                "meetings_today": 1,
            }
        )


class RowCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


def test_dashboard_uses_database_counts_and_calculates_only_progress():
    conn = DashboardConnection()
    config = SimpleNamespace(
        app_timezone="Africa/Addis_Ababa", default_daily_call_target=40, max_call_attempts=8
    )

    metrics = runtime.crm.dashboard(conn, 77, config)

    assert metrics["total_contacts"] == 120
    assert metrics["overdue"] == 2
    assert metrics["daily_target"] == 40
    assert metrics["target_progress"] == 25
    assert len(conn.queries) == 2
    metrics_sql, params = conn.queries[1]
    assert "count(*) from call_logs" in metrics_sql
    assert "count(*) from contacts" in metrics_sql
    assert "connection_result = 'answered'" in metrics_sql
    assert (
        "lead_status not in ('do_not_contact','converted','disqualified','not_interested')"
        in metrics_sql
    )
    assert len(params) == 11


class ReminderConnection:
    def __init__(self, row):
        self.row = row
        self.statements = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.lower().split())
        self.statements.append((normalized, params))
        if normalized.startswith("select d.follow_up_id"):
            return RowCursor(self.row)
        return RowCursor(None)


class FailedBot:
    async def send_message(self, *args, **kwargs):
        raise TelegramError("simulated Telegram outage")


@pytest.mark.asyncio
async def test_failed_reminder_notification_is_recorded_for_retry(monkeypatch):
    import contextlib
    from uuid import uuid4

    follow_up_id = uuid4()
    connection = ReminderConnection(
        {
            "follow_up_id": follow_up_id,
            "telegram_user_id": 77,
            "scheduled_at": datetime(2026, 10, 16, 8, tzinfo=UTC),
            "follow_up_type": "CALL_BACK",
            "contact_id": uuid4(),
            "name": "Alem",
            "phone_number": "+251911234567",
            "timezone": "Africa/Addis_Ababa",
        }
    )
    monkeypatch.setattr(worker_module, "connection", lambda: contextlib.nullcontext(connection))

    assert await worker_module._send_due_reminders(FailedBot(), limit=1) == 0

    updates = [
        (sql, params)
        for sql, params in connection.statements
        if sql.startswith("update reminder_deliveries")
    ]
    assert len(updates) == 2
    assert "status='sending'" in updates[0][0]
    assert updates[0][1] == (follow_up_id,)
    assert "status='failed'" in updates[1][0]
    assert updates[1][1] == ("TelegramError", follow_up_id)


@pytest.mark.asyncio
async def test_export_worker_generates_and_sends_file_outside_webhook(monkeypatch, tmp_path):
    import contextlib
    from uuid import uuid4

    export_id = uuid4()
    export_path = tmp_path / "queued.csv"
    job = {
        "id": export_id,
        "telegram_user_id": 77,
        "chat_id": 77,
        "export_format": "csv",
        "filter_name": "eligible",
        "campaign_id": None,
        "date_from": date(2026, 10, 1),
        "date_to_exclusive": date(2026, 11, 1),
        "timezone": "Africa/Addis_Ababa",
        "attempts": 1,
    }

    def build_export(action, config):
        assert action["date_from"] == "2026-10-01"
        assert action["date_to"] == "2026-11-01"
        export_path.write_text("contact_id,name\n", encoding="utf-8")
        return export_path, 1

    class ExportConnection:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=()):
            self.statements.append((" ".join(sql.lower().split()), params))
            return RowCursor(None)

    class ExportBot:
        def __init__(self):
            self.sent = None

        async def send_document(self, chat_id, document, filename, caption):
            self.sent = (chat_id, Path(document).exists(), filename, caption)

    connection = ExportConnection()
    bot = ExportBot()
    monkeypatch.setattr(worker_module, "_claim_export", lambda: job)
    monkeypatch.setattr(runtime, "_build_export", build_export)
    monkeypatch.setattr(worker_module, "connection", lambda: contextlib.nullcontext(connection))

    assert await worker_module._process_export(bot, SimpleNamespace(export_max_rows=50000)) is True
    assert bot.sent == (77, True, "crm_export.csv", "CSV export · 1 contacts")
    assert not export_path.exists()
    assert "status='completed'" in connection.statements[0][0]
    assert connection.statements[0][1] == (export_id,)
