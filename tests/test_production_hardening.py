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
from realestate_crm.handlers import (
    _CALL_CONFIRMATION_CODES,
    _call_confirmation_callback,
    _decode_outcome_callback,
)
from realestate_crm.services import outbox
from realestate_crm.telegram_ui import (
    OUTCOMES,
    TELEGRAM_CALLBACK_DATA_MAX_BYTES,
    button,
    compact_callback_data,
    expand_callback_data,
    outcomes_keyboard,
)


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


def test_callback_data_compacts_uuid_fields_and_enforces_telegram_limit():
    record_id = uuid4()
    original = f"p4:manage:{record_id}:{record_id}"

    compacted = compact_callback_data(original)
    assert len(compacted.encode("utf-8")) <= TELEGRAM_CALLBACK_DATA_MAX_BYTES
    assert "~" in compacted
    assert expand_callback_data(compacted) == original
    assert button("Open", original).callback_data == compacted

    with pytest.raises(ValueError, match="64-byte limit"):
        button("Invalid", "x" * (TELEGRAM_CALLBACK_DATA_MAX_BYTES + 1))


def test_every_call_outcome_button_fits_telegram_and_uses_valid_category_code():
    contact_id = uuid4()
    category_codes = {
        "CONNECTION": "c",
        "SALES_INTEREST": "s",
        "PROPERTY_REQUIREMENTS": "p",
        "FOLLOW_UP": "f",
        "DISQUALIFICATION": "d",
    }

    for category, choices in OUTCOMES.items():
        markup = outcomes_keyboard(str(contact_id), category)
        actual = markup.inline_keyboard[:-1]
        assert len(actual) == len(choices)
        for index, row in enumerate(actual):
            callback_data = row[0].callback_data
            assert callback_data == f"o:{contact_id}:{category_codes[category]}:{index}"
            assert len(callback_data.encode("utf-8")) <= TELEGRAM_CALLBACK_DATA_MAX_BYTES
            outcome = choices[index][1]
            assert _decode_outcome_callback(category_codes[category], str(index)) == (
                category,
                outcome,
            )
            assert _decode_outcome_callback(category, outcome) == (category, outcome)


def test_long_call_confirmation_callbacks_are_compact_and_decode_to_each_result():
    contact_id = uuid4()
    for code, (category, outcome) in _CALL_CONFIRMATION_CODES.items():
        callback_data = _call_confirmation_callback(contact_id, category, outcome)
        assert callback_data == f"cc:{contact_id}:{code}"
        assert len(callback_data.encode("utf-8")) <= TELEGRAM_CALLBACK_DATA_MAX_BYTES


@pytest.mark.asyncio
async def test_duplicate_callback_acknowledgements_are_sent_once(monkeypatch):
    acknowledged = []

    class FakeBot:
        def __init__(self, token):
            self.token = token

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def answer_callback_query(self, **kwargs):
            acknowledged.append(kwargs["callback_query_id"])

    monkeypatch.setattr(runtime, "Bot", FakeBot)
    settings = SimpleNamespace(telegram_bot_token=SecretStr("token"))
    actions = [
        {"type": "answer", "callback_id": "same"},
        {"type": "send", "chat_id": 77, "text": "Updated"},
        {"type": "answer", "callback_id": "same"},
        {"type": "answer", "callback_id": "other"},
    ]

    await runtime._send_callback_answers(actions, settings)

    assert acknowledged == ["same", "other"]


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
