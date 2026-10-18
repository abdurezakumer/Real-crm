from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError
from telegram import InlineKeyboardMarkup
from telegram.error import TelegramError

from api import ready
from realestate_crm import runtime
from realestate_crm.config import Settings
from realestate_crm.services import outbox


def valid_settings(**overrides):
    values = {
        "telegram_bot_token": "123456:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN",
        "telegram_webhook_secret": "Wb1_f9Rs2-Qk7tLm3xV8aN5pD0yHc6eZ",
        "telegram_webhook_url": "https://example.test/api/webhook",
        "authorized_telegram_user_id": 77,
        "supabase_url": "https://example.supabase.co",
        "supabase_service_role_key": "service-role",
        "supabase_database_url": "postgresql://test:test@localhost:5432/test",
        "follow_up_cron_secret": "C4sk_7Bq9wM2xP5rT8vY1nH6dJ0aL3fG",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("telegram_webhook_secret", "short"),
        ("telegram_webhook_secret", "bad " + "w" * 32),
        ("telegram_bot_token", "test-token"),
        ("follow_up_cron_secret", "short"),
        ("telegram_webhook_url", "http://example.test/api/webhook"),
        ("telegram_webhook_url", "https://user:pass@example.test/hook"),
        ("supabase_url", "http://remote.example"),
        (
            "supabase_database_url",
            "postgresql://crm:private@db.example.test:5432/crm",
        ),
    ],
)
def test_production_settings_reject_weak_secrets_and_insecure_webhook_config(field, value):
    with pytest.raises(ValidationError):
        valid_settings(**{field: value})


def test_readiness_reports_database_and_required_schema_without_exposing_errors(monkeypatch):
    class ReadinessConnection:
        def execute(self, sql):
            assert "telegram_outbox" in sql
            return SimpleNamespace(fetchone=lambda: {"ready": True})

    monkeypatch.setattr(ready, "get_settings", lambda: object())
    monkeypatch.setattr(ready, "connection", lambda: nullcontext(ReadinessConnection()))
    status, body = ready.readiness_status()
    assert status == 200
    assert body["ready"] is True

    def unavailable_connection():
        raise OSError("secret-bearing database detail")

    monkeypatch.setattr(ready, "connection", unavailable_connection)
    status, body = ready.readiness_status()
    assert status == 503
    assert body == {"ok": False, "ready": False, "service": "real-estate-calling-crm"}
    assert "secret-bearing" not in str(body)


def test_outbox_serializes_markup_and_keeps_callback_acknowledgements_immediate():
    class OutboxConnection:
        def __init__(self):
            self.executions = []

        def execute(self, sql, params):
            self.executions.append((" ".join(sql.lower().split()), params))

    conn = OutboxConnection()
    queued = outbox.enqueue_actions(
        conn,
        telegram_user_id=77,
        update_id=123,
        actions=[
            {"type": "answer", "callback_id": "callback-id", "text": ""},
            {
                "type": "send",
                "chat_id": 77,
                "text": "Open workspace",
                "markup": InlineKeyboardMarkup([[{"text": "Workspace", "callback_data": "menu"}]]),
            },
        ],
    )
    assert queued == 1
    sql, params = conn.executions[0]
    assert "insert into telegram_outbox" in sql
    assert params[:4] == (77, 123, 1, "SEND")
    assert '"inline_keyboard"' in params[4]


def test_outbox_failure_uses_bounded_retry_and_terminal_status():
    class OutboxConnection:
        def __init__(self):
            self.params = None

        def execute(self, sql, params):
            self.params = params

    conn = OutboxConnection()
    row = {"id": uuid4(), "attempts": 1}
    outbox.mark_failed(conn, row, TelegramError("private response"))
    assert conn.params == ("QUEUED", 15, "TelegramError", row["id"])

    row["attempts"] = 8
    outbox.mark_failed(conn, row, TelegramError("private response"))
    assert conn.params[0] == "FAILED"
    assert conn.params[1] <= 900


@pytest.mark.asyncio
async def test_outbox_send_failure_is_recorded_for_worker_retry(monkeypatch):
    outbox_id = uuid4()

    class FakeConnection:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            self.statements.append((normalized, params))
            if normalized.startswith("with exhausted as"):
                return SimpleNamespace(
                    fetchall=lambda: [
                        {
                            "id": outbox_id,
                            "action_type": "SEND",
                            "payload": {"type": "send", "chat_id": 77, "text": "test"},
                            "attempts": 1,
                        }
                    ]
                )
            return SimpleNamespace(rowcount=0)

    conn = FakeConnection()

    class FailedBot:
        def __init__(self, token):
            self.token = token

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send_message(self, **kwargs):
            raise TelegramError("temporary Telegram failure")

    monkeypatch.setattr(runtime, "connection", lambda: nullcontext(conn))
    monkeypatch.setattr(runtime, "Bot", FailedBot)
    settings = SimpleNamespace(telegram_bot_token=SecretStr("token"))

    sent = await runtime._drain_telegram_outbox(settings, limit=1)
    assert sent == 0
    failed = [
        params
        for sql, params in conn.statements
        if sql.startswith("update telegram_outbox set status=%s")
    ]
    assert failed == [("QUEUED", 15, "TelegramError", outbox_id)]
