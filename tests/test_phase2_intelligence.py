import json
from contextlib import nullcontext
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from realestate_crm.config import Settings
from realestate_crm.handlers import _get_input
from realestate_crm.models import PropertyInput, PropertyInterestInput
from realestate_crm.phase2_handlers import handle_callback
from realestate_crm.services import ai, intelligence
from realestate_crm.services.intelligence import (
    derive_qualification,
    explain_lead_score,
    match_property,
    reporting_bounds,
)
from realestate_crm.telegram_ui import lead_keyboard


def make_settings(**overrides):
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


def buyer(**overrides):
    values = {
        "property_type": "APARTMENT",
        "preferred_location": "Bole",
        "accepted_locations": [],
        "min_budget": Decimal("1000000"),
        "max_budget": Decimal("2000000"),
        "currency": "ETB",
        "min_bedrooms": 2,
        "max_bedrooms": 2,
        "preferred_payment_plan": "INSTALLMENT",
        "expected_timeline": "WITHIN_6_MONTHS",
    }
    values.update(overrides)
    return values


def prop(**overrides):
    values = {
        "property_type": "APARTMENT",
        "location": "Bole",
        "sub_location": "",
        "price": Decimal("1500000"),
        "currency": "ETB",
        "bedrooms": 2,
        "available_payment_plans": ["INSTALLMENT"],
        "payment_plans_verified": True,
        "construction_status": "READY_FOR_DELIVERY",
        "availability_status": "AVAILABLE",
    }
    values.update(overrides)
    return values


def test_buyer_profile_validation_accepts_partial_unknown_fields_and_rejects_bad_ranges():
    incomplete = PropertyInterestInput(currency="USD", preferred_location="  Bole  ")
    assert incomplete.property_type is None
    assert incomplete.max_budget is None
    assert incomplete.preferred_location == "  Bole  "

    with pytest.raises(ValidationError, match="Minimum budget cannot exceed maximum budget"):
        PropertyInterestInput(min_budget=200, max_budget=100)
    with pytest.raises(ValidationError, match="Minimum bedrooms cannot exceed maximum bedrooms"):
        PropertyInterestInput(min_bedrooms=3, max_bedrooms=1)


def test_property_validation_accepts_unknown_delivery_and_rejects_invalid_price():
    listing = PropertyInput(title="Apartment 1", property_type="APARTMENT", currency="ETB")
    assert listing.delivery_date is None
    with pytest.raises(ValidationError):
        PropertyInput(
            title="Apartment 1",
            property_type="APARTMENT",
            currency="ETB",
            price=100,
            minimum_negotiable_price=120,
        )
    with pytest.raises(ValidationError):
        PropertyInput(
            title="Apartment 2",
            property_type="APARTMENT",
            currency="ETB",
            price=Decimal("123456789012345678"),
        )


def test_property_inventory_create_edit_and_availability_changes_are_persisted():
    listing = PropertyInput(
        title="Apartment 1", property_type="APARTMENT", currency="ETB"
    ).model_dump()
    property_id = uuid4()

    class PropertyConnection:
        def __init__(self):
            self.calls = []
            self.old_status = "AVAILABLE"

        def execute(self, sql, params):
            normalized = " ".join(sql.lower().split())
            self.calls.append((normalized, params))
            if normalized.startswith("select * from properties"):
                return SimpleNamespace(
                    fetchone=lambda: {
                        "id": property_id,
                        "availability_status": self.old_status,
                    }
                )
            if normalized.startswith("update properties set availability_status"):
                self.old_status = params[0]
                return SimpleNamespace(
                    fetchone=lambda: {
                        "id": property_id,
                        "availability_status": self.old_status,
                    }
                )
            return SimpleNamespace(fetchone=lambda: {"id": property_id, **listing})

    conn = PropertyConnection()
    created = intelligence.save_property(conn, listing)
    assert created["id"] == property_id
    assert conn.calls[-1][0].startswith("insert into properties")

    edited = intelligence.save_property(conn, listing, property_id)
    assert edited["id"] == property_id
    assert conn.calls[-1][0].startswith("update properties set")

    changed = intelligence.update_availability(conn, property_id, "SOLD", 77)
    assert changed["availability_status"] == "SOLD"
    assert any("property_availability" in sql for sql, _ in conn.calls)


def test_exact_property_match_has_reproducible_full_score():
    first = match_property(buyer(), prop(), today=date(2026, 1, 1))
    second = match_property(buyer(), prop(), today=date(2026, 1, 1))
    assert first == second
    assert first["score"] == 100
    assert first["category"] == "EXCELLENT"
    assert first["rejected"] is False


@pytest.mark.parametrize(
    ("buyer_values", "property_values", "allow_near", "expected_rejected", "expected_score"),
    [
        ({"property_type": "HOUSE_VILLA"}, {}, False, True, 85),
        ({"preferred_location": "Kazanchis"}, {}, False, True, 75),
        ({"max_budget": Decimal("1000000")}, {"price": Decimal("1200000")}, False, True, 65),
        ({"max_budget": Decimal("1000000")}, {"price": Decimal("1050000")}, True, False, 82),
        ({"min_budget": None, "max_budget": None}, {}, False, False, 65),
        ({}, {"available_payment_plans": ["FULL_CASH"]}, False, False, 90),
        ({}, {"availability_status": "SOLD"}, False, True, 100),
    ],
)
def test_property_matching_hard_constraints_and_missing_information(
    buyer_values, property_values, allow_near, expected_rejected, expected_score
):
    result = match_property(
        buyer(**buyer_values),
        prop(**property_values),
        allow_near_budget=allow_near,
        today=date(2026, 1, 1),
    )
    assert result["rejected"] is expected_rejected
    assert result["score"] == expected_score


def test_alternative_location_can_match_and_missing_preferences_never_earn_points():
    accepted = match_property(
        buyer(accepted_locations=["Kazanchis"]), prop(location="KAZANCHIS"), today=date(2026, 1, 1)
    )
    unknown = match_property(
        buyer(
            property_type=None,
            preferred_location=None,
            accepted_locations=[],
            min_budget=None,
            max_budget=None,
            min_bedrooms=None,
            max_bedrooms=None,
            preferred_payment_plan=None,
            expected_timeline=None,
        ),
        prop(),
        today=date(2026, 1, 1),
    )
    assert accepted["rejected"] is False
    assert accepted["score"] == 95
    assert unknown["score"] == 0
    assert unknown["partial"] is True


def test_qualification_requires_explicit_checklist_and_promotes_visit_or_near_timeline():
    incomplete = derive_qualification({"property_requirement_known": True})
    assert incomplete["status"] == "IN_PROGRESS"
    assert "Budget recorded or declined" in incomplete["missing"]

    complete = derive_qualification(
        {
            "property_requirement_known": True,
            "budget_declined": True,
            "timeline_confirmed": True,
            "expected_timeline": "WITHIN_3_MONTHS",
            "genuine_interest": True,
            "next_action": "Call Friday",
        }
    )
    assert complete["status"] == "HIGH_PRIORITY_QUALIFIED"

    disqualified = derive_qualification({"qualification_status": "DISQUALIFIED"})
    assert disqualified["status"] == "DISQUALIFIED"


def test_lead_score_is_normalized_explained_and_independent_of_repeated_no_answer_count():
    facts = {
        "genuine_interest": True,
        "interest_level": "HIGH",
        "property_type": "APARTMENT",
        "preferred_location": "Bole",
        "budget_known": True,
        "expected_timeline": "WITHIN_1_MONTH",
        "meaningful_engagement": True,
        "next_action": True,
        "recent_positive": True,
    }
    first = explain_lead_score({**facts, "total_call_attempts": 1})
    after_repeated_no_answers = explain_lead_score({**facts, "total_call_attempts": 8})
    assert first == after_repeated_no_answers
    assert first["score"] == 100
    assert first["category"] == "A"
    assert "Confirmed purchase interest" in first["reasons"]


def test_reporting_periods_use_local_midnight_and_custom_inclusive_dates():
    now = datetime(2026, 10, 16, 7, 30, tzinfo=UTC)
    start, end, label_start, label_end = reporting_bounds("today", "Africa/Addis_Ababa", now)
    assert start == datetime(2026, 10, 15, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 16, 21, 0, tzinfo=UTC)
    assert (label_start, label_end) == ("2026-10-16", "2026-10-16")

    start, end, label_start, label_end = reporting_bounds(
        "custom", "Africa/Addis_Ababa", now, date(2026, 10, 1), date(2026, 10, 3)
    )
    assert (label_start, label_end) == ("2026-10-01", "2026-10-03")
    assert (end - start).days == 3
    with pytest.raises(ValueError, match="valid and ordered"):
        reporting_bounds("custom", "UTC", now, date(2026, 10, 4), date(2026, 10, 3))


def test_ai_must_be_explicitly_configured_when_enabled():
    missing = make_settings(ai_enabled=True)
    assert missing.ai_enabled is False
    assert "AI requires AI_BASE_URL" in missing.ai_configuration_error

    insecure = make_settings(
        ai_enabled=True,
        ai_base_url="http://provider.test/v1",
        ai_api_key=SecretStr("provider-secret"),
        ai_model="administrator-selected-model",
    )
    assert insecure.ai_enabled is False
    assert "must use HTTPS" in insecure.ai_configuration_error


def test_ai_queue_validates_input_schema_and_keeps_original_notes_only_in_database():
    class AIQueueConnection:
        def __init__(self):
            self.params = None

        def execute(self, _sql, params):
            self.params = params
            return SimpleNamespace(fetchone=lambda: {"id": uuid4()})

    settings = make_settings(
        ai_enabled=True,
        ai_base_url="https://provider.test/v1",
        ai_api_key="provider-key",
        ai_model="model",
    )
    conn = AIQueueConnection()
    with pytest.raises(ai.AIError, match="input did not match"):
        ai.queue_ai_job(
            conn,
            settings=settings,
            user_id=77,
            chat_id=77,
            job_type="CALL_SCRIPT",
            payload={},
        )
    assert conn.params is None

    ai.queue_ai_job(
        conn,
        settings=settings,
        user_id=77,
        chat_id=77,
        job_type="STRUCTURE_NOTES",
        payload={
            "notes": "Phone redacted; budget 15 million ETB.",
            "existing_preferences": {},
            "source_note": "Phone +251911234567; budget 15 million ETB.",
        },
    )
    stored_payload = json.loads(conn.params[-1])
    assert stored_payload["source_note"].startswith("Phone +251")
    assert "source_note" not in ai.validate_input("STRUCTURE_NOTES", stored_payload)


def test_recommendation_rejection_stores_agent_reason():
    class RecommendationConnection:
        def execute(self, sql, params):
            self.sql = " ".join(sql.lower().split())
            self.params = params
            return SimpleNamespace(fetchone=lambda: {"id": params[-1]})

    recommendation_id = uuid4()
    conn = RecommendationConnection()
    assert (
        intelligence.update_recommendation(
            conn, recommendation_id, "REJECTED", "Agent rejected match", "Too far from workplace"
        )
        is True
    )
    assert "recommendation_status=%s" in conn.sql
    assert conn.params == (
        "REJECTED",
        "Agent rejected match",
        "Too far from workplace",
        recommendation_id,
    )


@pytest.mark.asyncio
async def test_ai_disabled_does_not_make_provider_request():
    settings = SimpleNamespace(ai_enabled=False)
    with pytest.raises(ai.AIError, match="disabled"):
        await ai.generate_ai_result(settings, "CALL_SCRIPT", {})


class FakeResponse:
    def __init__(self, status_code=200, content=None):
        self.status_code = status_code
        self.content = content

    def json(self):
        return {
            "choices": [{"message": {"content": self.content}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        }


class FakeClient:
    def __init__(self, *, response=None, error=None, capture=None, **_kwargs):
        self.response = response
        self.error = error
        self.capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, *, json, headers):
        if self.capture is not None:
            self.capture.update(url=url, body=json, headers=headers)
        if self.error:
            raise self.error
        return self.response


@pytest.mark.asyncio
async def test_ai_provider_validates_schema_and_sends_only_configured_model(monkeypatch):
    captured = {}
    output = {
        "opening": "Hello.",
        "reason_for_calling": "Discuss an apartment.",
        "questions": ["What are you looking for?"],
        "next_step": "May I send details?",
        "alternatives": [],
    }
    monkeypatch.setattr(
        ai.httpx,
        "AsyncClient",
        lambda **kwargs: FakeClient(
            response=FakeResponse(content=json.dumps(output)), capture=captured, **kwargs
        ),
    )
    settings = make_settings(
        ai_enabled=True,
        ai_base_url="https://provider.test/v1",
        ai_api_key="secret-provider-key",
        ai_model="administrator-model",
    )
    result, usage = await ai.generate_ai_result(settings, "CALL_SCRIPT", {"objective": "intro"})
    assert result["opening"] == "Hello."
    assert usage == {"input_tokens": 12, "output_tokens": 7}
    assert captured["url"] == "https://provider.test/v1/chat/completions"
    assert captured["body"]["model"] == "administrator-model"
    assert captured["body"]["max_tokens"] == settings.ai_max_output_chars // 4
    assert captured["headers"]["Authorization"] == "Bearer secret-provider-key"


@pytest.mark.asyncio
async def test_structured_note_source_copy_is_saved_locally_but_not_sent_to_provider(monkeypatch):
    captured = {}
    output = {"preferred_location": "Bole", "missing_information": []}
    monkeypatch.setattr(
        ai.httpx,
        "AsyncClient",
        lambda **kwargs: FakeClient(
            response=FakeResponse(content=json.dumps(output)), capture=captured, **kwargs
        ),
    )
    settings = make_settings(
        ai_enabled=True,
        ai_base_url="https://provider.test/v1",
        ai_api_key="provider-key",
        ai_model="model",
    )
    _, _ = await ai.generate_ai_result(
        settings,
        "STRUCTURE_NOTES",
        {
            "notes": "Phone [phone removed], budget 15 million ETB.",
            "existing_preferences": {},
            "source_note": "Phone +251911234567, budget 15 million ETB.",
        },
    )
    provider_input = json.loads(captured["body"]["messages"][1]["content"])
    assert "source_note" not in provider_input
    assert "251911234567" not in provider_input["notes"]
    assert "15 million ETB" in provider_input["notes"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_retryable", "message"),
    [
        (FakeResponse(status_code=429), True, "HTTP 429"),
        (FakeResponse(content="not-json"), True, "schema"),
        (FakeResponse(content=json.dumps({"opening": "missing required fields"})), True, "schema"),
        (FakeResponse(content=json.dumps({"opening": "x" * 501})), False, "length limit"),
    ],
)
async def test_ai_provider_handles_rate_limit_invalid_json_missing_fields_and_long_output(
    monkeypatch, response, expected_retryable, message
):
    monkeypatch.setattr(
        ai.httpx, "AsyncClient", lambda **kwargs: FakeClient(response=response, **kwargs)
    )
    settings = make_settings(
        ai_enabled=True,
        ai_base_url="https://provider.test/v1",
        ai_api_key="provider-key",
        ai_model="model",
        ai_max_output_chars=500,
    )
    with pytest.raises(ai.AIError, match=message) as error:
        await ai.generate_ai_result(settings, "CALL_SCRIPT", {"objective": "intro"})
    assert error.value.retryable is expected_retryable


@pytest.mark.asyncio
async def test_ai_provider_timeout_is_bounded_retryable_failure(monkeypatch):
    monkeypatch.setattr(
        ai.httpx,
        "AsyncClient",
        lambda **kwargs: FakeClient(error=httpx.ReadTimeout("timed out"), **kwargs),
    )
    settings = make_settings(
        ai_enabled=True,
        ai_base_url="https://provider.test/v1",
        ai_api_key="provider-key",
        ai_model="model",
        ai_request_timeout_seconds=3,
    )
    with pytest.raises(ai.AIError, match="timed out") as error:
        await ai.generate_ai_result(settings, "CALL_SCRIPT", {"objective": "intro"})
    assert error.value.retryable is True


def test_ai_note_schema_does_not_accept_unordered_budgets_or_unsupported_fields():
    with pytest.raises(ValidationError):
        ai.StructuredNotesOutput(min_budget=200, max_budget=100)
    with pytest.raises(ValidationError):
        ai.StructuredNotesOutput(property_type="CRYPTOCURRENCY")
    with pytest.raises(ValidationError):
        ai.CallScriptOutput(
            opening="Hi",
            reason_for_calling="Reason",
            questions=["Question?"],
            next_step="Next",
            unexpected="arbitrary",
        )


def test_user_confirmation_preserves_original_call_notes(monkeypatch):
    from realestate_crm import phase2_handlers

    saved = []
    monkeypatch.setattr(phase2_handlers.crm, "save_note", lambda *_args: saved.append(_args))
    monkeypatch.setattr(phase2_handlers, "_property_profile", lambda *_args: {})
    monkeypatch.setattr(phase2_handlers, "_save_profile", lambda *_args: None)
    monkeypatch.setattr(
        phase2_handlers.intelligence, "save_qualification", lambda *_args, **_kwargs: None
    )

    class NoteConnection:
        def __init__(self):
            self.sql = []

        def execute(self, sql, _params):
            self.sql.append(" ".join(sql.lower().split()))
            return SimpleNamespace(fetchone=lambda: None)

    contact_id = uuid4()
    job = {
        "id": uuid4(),
        "contact_id": contact_id,
        "job_type": "STRUCTURE_NOTES",
        "input_payload": {
            "notes": "Asked for a two bedroom apartment in Bole.",
            "source_note": "Asked for a two bedroom apartment in Bole.",
        },
        "result_payload": {"property_type": "APARTMENT", "preferred_location": "Bole"},
    }
    conn = NoteConnection()
    phase2_handlers._save_structured_notes(conn, 77, SimpleNamespace(), job)
    assert saved[0][2] == "Asked for a two bedroom apartment in Bole."
    assert saved[0][3] == "AI_STRUCTURED_SOURCE"
    assert any("status='cancelled'" in sql for sql in conn.sql)


@pytest.mark.parametrize("operation", ["confirm", "discard"])
def test_user_confirmation_or_rejection_is_an_explicit_callback(monkeypatch, operation):
    from realestate_crm import phase2_handlers

    job_id = uuid4()
    contact_id = uuid4()
    job = {
        "id": job_id,
        "contact_id": contact_id,
        "job_type": "STRUCTURE_NOTES",
        "status": "COMPLETED",
        "result_payload": {},
        "input_payload": {"notes": "Raw notes"},
    }
    calls = []
    monkeypatch.setattr(
        phase2_handlers, "_save_structured_notes", lambda *_args: calls.append("confirmed")
    )
    monkeypatch.setattr(
        phase2_handlers.crm,
        "get_contact",
        lambda *_args: {
            "contact_eligibility": "ELIGIBLE",
            "lead_status": "INTERESTED",
            "conversion_state": "NOT_CONVERTED",
            "qualification_status": "IN_PROGRESS",
        },
    )
    message = SimpleNamespace(chat_id=77, message_id=10)
    query = SimpleNamespace(id="callback-confirm", message=message)
    update = SimpleNamespace(callback_query=query, effective_message=message)

    class AIActionConnection:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params):
            normalized = " ".join(sql.lower().split())
            self.statements.append((normalized, params))
            if normalized.startswith("select * from ai_jobs"):
                return SimpleNamespace(fetchone=lambda: job)
            return SimpleNamespace(fetchone=lambda: None)

    conn = AIActionConnection()
    actions = []
    assert (
        handle_callback(
            conn,
            update,
            77,
            SimpleNamespace(ai_enabled=True),
            3,
            actions,
            f"ai:{operation}:{job_id}",
        )
        is True
    )
    if operation == "confirm":
        assert calls == ["confirmed"]
    else:
        assert calls == []
        assert any("status='cancelled'" in sql for sql, _ in conn.statements)


def test_ai_request_reservation_serializes_daily_quota_checks(monkeypatch):
    from realestate_crm import worker

    job = {"id": uuid4(), "telegram_user_id": 77, "job_type": "CALL_SCRIPT", "attempts": 1}

    class UsageConnection:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params):
            normalized = " ".join(sql.lower().split())
            self.sql.append((normalized, params))
            if normalized.startswith("select count(*) as used"):
                return SimpleNamespace(fetchone=lambda: {"used": 0})
            if normalized.startswith("insert into ai_usage"):
                return SimpleNamespace(fetchone=lambda: {"id": 1})
            return SimpleNamespace(fetchone=lambda: None)

    conn = UsageConnection()
    monkeypatch.setattr(worker, "connection", lambda: nullcontext(conn))
    assert worker._reserve_ai_usage(job, 20) is True
    assert conn.sql[0][0] == "select pg_advisory_xact_lock(%s)"
    assert "'reserved'" in conn.sql[2][0]


def test_malformed_script_callback_is_rejected_without_crashing_or_database_access():
    contact_id = uuid4()
    message = SimpleNamespace(chat_id=77, message_id=10)
    query = SimpleNamespace(id="callback-1", message=message)
    update = SimpleNamespace(callback_query=query, effective_message=message)
    actions = []
    assert (
        handle_callback(
            SimpleNamespace(),
            update,
            77,
            SimpleNamespace(ai_enabled=True),
            1,
            actions,
            f"p2:sc:{contact_id}:forged-language:en",
        )
        is True
    )
    assert any("no longer valid" in (action.get("text") or "") for action in actions)


def test_disqualified_buyer_card_hides_call_and_ai_actions():
    contact = {
        "id": uuid4(),
        "name": "Test Buyer",
        "phone_number": "+251911234567",
        "normalized_phone": "+251911234567",
        "contact_eligibility": "ELIGIBLE",
        "lead_status": "INTERESTED",
        "qualification_status": "DISQUALIFIED",
        "conversion_state": "NOT_CONVERTED",
        "interest_level": "HIGH",
        "total_call_attempts": 1,
    }
    callbacks = {
        button.callback_data
        for row in lead_keyboard(contact, None, ai_enabled=True).inline_keyboard
        for button in row
    }
    assert not any(value and value.startswith("call:") for value in callbacks)
    assert not any(value and value.startswith("p2:script:") for value in callbacks)


def test_ai_context_redacts_selected_contact_phone_without_removing_budget():
    from realestate_crm.phase2_handlers import _redact_contact_numbers

    note = "Call +251 911 234 567; budget is 15,000,000 ETB."
    cleaned = _redact_contact_numbers(
        note,
        {"phone_number": "+251911234567", "normalized_phone": "+251911234567"},
    )
    assert "[phone removed]" in cleaned
    assert "251911234567" not in cleaned
    assert "15,000,000 ETB" in cleaned


def test_durable_phase2_input_state_is_written_to_database():
    from realestate_crm.phase2_handlers import _state

    class StateConnection:
        def execute(self, sql, params):
            self.sql = " ".join(sql.lower().split())
            self.params = params
            return SimpleNamespace(fetchone=lambda: None)

    conn = StateConnection()
    contact_id = uuid4()
    _state(conn, 77, "P2_PROFILE_INPUT", contact_id, {"field": "preferred_location"})
    assert "insert into user_input_state" in conn.sql
    assert "on conflict (telegram_user_id) do update" in conn.sql
    assert json.loads(conn.params[3]) == {"field": "preferred_location"}
    assert conn.params[:3] == (77, "P2_PROFILE_INPUT", contact_id)

    stored = {"purpose": "P2_PROFILE_INPUT", "payload": {"field": "preferred_location"}}

    class ResumeConnection:
        def execute(self, sql, params):
            assert "expires_at>now()" in sql
            assert params == (77,)
            return SimpleNamespace(fetchone=lambda: stored)

    # A later serverless invocation reloads the same durable input state from PostgreSQL.
    assert _get_input(ResumeConnection(), 77) == stored


def test_phase2_cancel_clears_persisted_state_and_handles_user_input_cancellation():
    state = {"purpose": "P2_CUSTOM_REPORT", "contact_id": None}
    message = SimpleNamespace(chat_id=77, message_id=10)
    query = SimpleNamespace(id="callback-cancel", message=message)
    update = SimpleNamespace(callback_query=query, effective_message=message)

    class CancelConnection:
        def __init__(self):
            self.deleted = False

        def execute(self, sql, params):
            if "select purpose,contact_id" in sql:
                return SimpleNamespace(fetchone=lambda: state)
            if "delete from user_input_state" in sql:
                self.deleted = True
            return SimpleNamespace(fetchone=lambda: None)

    conn = CancelConnection()
    actions = []
    assert (
        handle_callback(
            conn, update, 77, SimpleNamespace(ai_enabled=False), 2, actions, "p2:cancel"
        )
        is True
    )
    assert conn.deleted is True
    assert any("cancelled" in (action.get("text") or "").lower() for action in actions)


def test_campaign_and_property_reports_use_aggregated_database_rows_and_zero_safe_rates():
    class ReportConnection:
        def __init__(self):
            self.calls = 0

        def execute(self, _sql, _params):
            self.calls += 1
            if self.calls == 1:
                row = {
                    "attempted": 0,
                    "unique_contacted": 0,
                    "answered": 0,
                    "interested": 0,
                    "meetings": 0,
                    "visits": 0,
                    "conversions": 0,
                    "scheduled": 0,
                    "completed": 0,
                    "missed": 0,
                    "overdue": 0,
                    "qualified": 0,
                }
                return SimpleNamespace(fetchone=lambda: row)
            if self.calls == 2:
                return SimpleNamespace(
                    fetchall=lambda: [
                        {
                            "id": uuid4(),
                            "name": "Campaign A",
                            "calls": 6,
                            "unique_contacted": 4,
                            "interested": 2,
                            "qualified": 1,
                            "conversions": 1,
                        }
                    ]
                )
            return SimpleNamespace(
                fetchall=lambda: [
                    {
                        "id": uuid4(),
                        "title": "Apartment",
                        "availability_status": "AVAILABLE",
                        "recommendations": 3,
                        "interested": 1,
                        "visits": 1,
                    }
                ]
            )

    from realestate_crm.services.intelligence import sales_intelligence_report

    report = sales_intelligence_report(ReportConnection(), "today", "UTC")
    assert report["answer_rate"] == 0
    assert report["interest_rate"] == 0
    assert report["campaigns"][0]["qualified_rate"] == 25.0
    assert report["campaigns"][0]["conversion_rate"] == 25.0
    assert report["properties"][0]["recommendations"] == 3
