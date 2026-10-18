from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from realestate_crm.models import NegotiationInput, ReservationInput, VisitInput
from realestate_crm.services import ai, pipeline
from realestate_crm.services.notifications import _message


def test_commission_amount_uses_decimal_half_up_rounding():
    assert pipeline.commission_amount("PERCENTAGE", "2.5", "123.45") == Decimal("3.09")
    assert pipeline.commission_amount("FIXED", "100.005", "123.45") == Decimal("100.01")

    with pytest.raises(ValueError, match="Commission rule"):
        pipeline.commission_amount("OTHER", 1, 100)


@pytest.mark.parametrize(
    ("asking", "offer", "asking_currency", "offer_currency", "difference", "percentage"),
    [
        ("100.00", "80", "ETB", "ETB", Decimal("20.00"), Decimal("20.00")),
        ("0", "0", "USD", "USD", Decimal("0.00"), None),
        (None, "80", "ETB", "ETB", None, None),
    ],
)
def test_price_difference_is_currency_aware_and_handles_missing_or_zero(
    asking, offer, asking_currency, offer_currency, difference, percentage
):
    result = pipeline.calculate_price_difference(asking, offer, asking_currency, offer_currency)
    assert result["difference"] == difference
    assert result["percentage_difference"] == percentage


def test_price_difference_refuses_cross_currency_comparisons():
    with pytest.raises(ValueError, match="different or unknown currencies"):
        pipeline.calculate_price_difference(100, 80, "ETB", "USD")


def test_negotiation_reservation_and_visit_inputs_validate_financial_and_time_data():
    with pytest.raises(ValidationError):
        NegotiationInput(offer_date="2026-10-12", offer_expiry_date="2026-10-11")
    with pytest.raises(ValidationError):
        NegotiationInput(current_discussed_price=100, down_payment=101)
    with pytest.raises(ValidationError):
        VisitInput(scheduled_at="2026-10-16 10:00:00")
    with pytest.raises(ValidationError):
        ReservationInput(
            reservation_date="2026-10-16",
            reservation_amount="1.239",
            currency="ETB",
            expires_at="2026-10-17T10:00:00",
        )


def test_stage_transition_requires_visit_evidence_before_writing_stage():
    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            self.statements.append(normalized)
            if normalized.startswith("select stage_code"):
                return Result({"stage_code": "VISIT_COMPLETED"})
            if normalized.startswith("select * from sales_opportunities"):
                return Result({"current_stage": "FOLLOW_UP", "final_outcome": None})
            if normalized.startswith("select id from property_visits"):
                return Result(None)
            raise AssertionError(f"unexpected SQL: {normalized}")

    conn = Connection()
    with pytest.raises(ValueError, match="[Rr]ecord the related visit completed first"):
        pipeline.update_stage(conn, "opportunity-id", "VISIT_COMPLETED", 77, 100)
    assert not any(sql.startswith("update sales_opportunities") for sql in conn.statements)


@pytest.mark.parametrize(
    "operation",
    [
        lambda conn: pipeline.close_lost(
            conn, "opportunity-id", "OTHER", None, 77, 100, confirmed=False
        ),
        lambda conn: pipeline.update_commission(
            conn, "commission-id", status="CONFIRMED", amount=None, actor_id=77, confirmed=False
        ),
        lambda conn: pipeline.set_sale_verification(
            conn, "sale-id", "VERIFIED", 77, confirmed=False
        ),
    ],
)
def test_financial_and_terminal_changes_require_confirmation_before_database_access(operation):
    class NoQueries:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("Database must not be touched before explicit confirmation")

    with pytest.raises(ValueError, match="confirmation"):
        operation(NoQueries())


def test_commission_cannot_be_marked_paid_without_a_payment_record():
    class Result:
        def fetchone(self):
            return {
                "commission_status": "CONFIRMED",
                "paid_amount": Decimal("0"),
                "calculated_amount": Decimal("100"),
            }

    class Connection:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            self.statements.append(normalized)
            return Result()

    conn = Connection()
    with pytest.raises(ValueError, match="payment record flow"):
        pipeline.update_commission(
            conn,
            "commission-id",
            status="PAID",
            amount=None,
            actor_id=77,
            confirmed=True,
        )
    assert not any(sql.startswith("update commission_records") for sql in conn.statements)


def test_commission_payment_cannot_exceed_approved_outstanding_balance():
    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            self.statements.append(normalized)
            if normalized.startswith("select * from crm_users"):
                return Result({"role": "SUPER_ADMIN", "account_status": "ACTIVE"})
            if normalized.startswith("select allowed from crm_user_permissions"):
                return Result(None)
            if normalized.startswith("select * from commission_records"):
                return Result(
                    {
                        "commission_status": "PARTIALLY_PAID",
                        "approved_amount": Decimal("100"),
                        "calculated_amount": Decimal("100"),
                        "paid_amount": Decimal("80"),
                    }
                )
            raise AssertionError(f"unexpected SQL: {normalized}")

    conn = Connection()
    with pytest.raises(ValueError, match="exceeds the approved outstanding amount"):
        pipeline.record_commission_payment(
            conn,
            "commission-id",
            amount=Decimal("25"),
            reference="receipt-123",
            actor_id=77,
            confirmed=True,
        )
    assert not any(sql.startswith("update commission_records") for sql in conn.statements)


def test_commission_payment_records_partial_amount_and_reference():
    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            self.statements.append((normalized, params))
            if normalized.startswith("select * from crm_users"):
                return Result({"role": "SUPER_ADMIN", "account_status": "ACTIVE"})
            if normalized.startswith("select allowed from crm_user_permissions"):
                return Result()
            if normalized.startswith("select * from commission_records"):
                return Result(
                    {
                        "commission_status": "CONFIRMED",
                        "approved_amount": Decimal("100"),
                        "calculated_amount": Decimal("100"),
                        "paid_amount": Decimal("0"),
                    }
                )
            if normalized.startswith("update commission_records"):
                return Result(
                    {
                        "commission_status": params[0],
                        "approved_amount": params[1],
                        "paid_amount": params[2],
                        "payment_reference": params[4],
                    }
                )
            return Result()

    conn = Connection()
    result = pipeline.record_commission_payment(
        conn,
        "commission-id",
        amount=Decimal("40.00"),
        reference="receipt-123",
        actor_id=77,
        confirmed=True,
    )
    assert result["commission_status"] == "PARTIALLY_PAID"
    assert result["paid_amount"] == Decimal("40.00")
    assert result["payment_reference"] == "receipt-123"
    assert any(
        sql.startswith("insert into commission_approval_history") for sql, _ in conn.statements
    )


def test_opportunity_ai_input_and_output_are_bounded_and_strict():
    payload = {
        "current_stage": "FOLLOW_UP",
        "buyer_requirements": {"location": "Bole"},
        "property_summary": {"availability_status": "AVAILABLE"},
        "recent_recorded_events": [{"type": "NOTE_ADDED"}] * 8,
    }
    validated = ai.validate_input("OPPORTUNITY_ASSIST", payload)
    assert len(validated["recent_recorded_events"]) == 8

    with pytest.raises(ai.AIError, match="required operation schema"):
        ai.validate_input("OPPORTUNITY_ASSIST", payload | {"phone_number": "+251900000000"})
    with pytest.raises(ai.AIError, match="required operation schema"):
        ai.validate_input(
            "OPPORTUNITY_ASSIST", payload | {"recent_recorded_events": [{"id": 1}] * 9}
        )

    output = ai.OpportunityAssistOutput(
        summary="Buyer is comparing two properties.",
        suggested_action="Ask which location is preferred.",
        reason="The latest note records two options.",
    )
    assert output.followup_draft is None
    with pytest.raises(ValidationError):
        ai.OpportunityAssistOutput(
            summary="ok",
            suggested_action="ok",
            reason="ok",
            unsupported="cannot be accepted",
        )


def test_followup_notification_escapes_content_and_warns_for_do_not_contact():
    scheduled = (datetime.now(UTC) + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S.%f")
    text = _message(
        {
            "notification_type": "FOLLOW_UP_SOON",
            "message_context": {
                "name": "<Buyer & Co>",
                "action": "CALL_BACK",
                "scheduled_at": scheduled,
            },
            "contact_restricted": True,
        },
        "Africa/Addis_Ababa",
    )
    assert "&lt;Buyer &amp; Co&gt;" in text
    assert "DO NOT CONTACT" in text
    assert "do not call or message" in text


def test_custom_followup_cannot_bypass_do_not_contact():
    contact_id = "contact-id"

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, sql, _params=()):
            if "from sales_opportunities" in sql:
                return Result(
                    {
                        "contact_id": contact_id,
                        "campaign_id": None,
                        "current_stage": "FOLLOW_UP",
                        "final_outcome": None,
                    }
                )
            if "from contacts" in sql:
                return Result(
                    {"contact_eligibility": "DO_NOT_CONTACT", "lead_status": "DO_NOT_CONTACT"}
                )
            raise AssertionError("An outbound follow-up must be rejected before insertion")

    with pytest.raises(ValueError, match="Do Not Contact"):
        pipeline.schedule_opportunity_followup(
            Connection(),
            "opportunity-id",
            datetime.now(UTC) + timedelta(days=1),
            "CUSTOM: ask about timing",
            77,
        )


def test_contact_level_reactivation_followup_rechecks_current_contact_eligibility():
    class Result:
        def fetchone(self):
            return {
                "campaign_id": None,
                "contact_eligibility": "INVALID_NUMBER",
                "lead_status": "NEW",
            }

    class Connection:
        def execute(self, sql, _params=()):
            if "from contacts" in sql:
                return Result()
            raise AssertionError("No follow-up should be inserted for an invalid number")

    with pytest.raises(ValueError, match="not eligible"):
        pipeline.schedule_contact_followup(
            Connection(),
            "contact-id",
            datetime.now(UTC) + timedelta(days=1),
            "CALL_BACK",
            77,
            reason="Reactivation follow-up",
        )


@pytest.mark.parametrize(
    ("lead_status", "conversion_state"),
    [
        ("DO_NOT_CONTACT", "NOT_CONVERTED"),
        ("NOT_INTERESTED", "NOT_CONVERTED"),
        ("DISQUALIFIED", "NOT_CONVERTED"),
        ("CONVERTED", "NOT_CONVERTED"),
        ("INTERESTED", "CONVERTED"),
    ],
)
def test_reactivation_call_now_rejects_suppressed_or_converted_contacts(
    lead_status, conversion_state
):
    class Result:
        def fetchone(self):
            return {
                "id": "recommendation-id",
                "contact_id": "contact-id",
                "status": "OPEN",
                "contact_eligibility": "ELIGIBLE",
                "lead_status": lead_status,
                "conversion_state": conversion_state,
                "normalized_phone": "+251900000000",
            }

    class Connection:
        def __init__(self):
            self.statements = []

        def execute(self, sql, _params=()):
            self.statements.append(" ".join(sql.lower().split()))
            return Result()

    conn = Connection()
    with pytest.raises(ValueError, match="not eligible for calling"):
        pipeline.act_on_reactivation(conn, "recommendation-id", "CALL_NOW", 77, confirmed=True)
    assert len(conn.statements) == 1
    assert "update reactivation_recommendations" not in conn.statements[0]


def test_pipeline_won_report_requires_verified_sale_record():
    class Result:
        def fetchone(self):
            return {"active": 0, "won": 0, "lost": 0}

        def fetchall(self):
            return []

    class Connection:
        def __init__(self):
            self.queries = []

        def execute(self, sql, _params=()):
            self.queries.append(" ".join(sql.lower().split()))
            return Result()

    conn = Connection()
    pipeline.report_pipeline(
        conn,
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert "s.verification_status='verified'" in conn.queries[0]
    assert "s.verified_at>=%s" in conn.queries[0]
