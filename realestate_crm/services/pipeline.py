"""Durable sales-pipeline, visit, negotiation, and closing operations."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from psycopg import Connection

from realestate_crm.models import (
    NegotiationInput,
    OpportunityInput,
    ReservationInput,
    SaleInput,
    VisitInput,
)
from realestate_crm.timeutils import local_day_bounds

OPPORTUNITY_STAGES = (
    "NEW_OPPORTUNITY",
    "BUYER_QUALIFICATION",
    "PROPERTY_MATCHING",
    "PROPERTY_INFORMATION_SHARED",
    "FOLLOW_UP",
    "VISIT_SCHEDULED",
    "VISIT_COMPLETED",
    "NEGOTIATION",
    "RESERVATION_IN_PROGRESS",
    "BOOKED",
    "WON",
    "LOST",
    "ON_HOLD",
)
ACTIVE_STAGES = frozenset(OPPORTUNITY_STAGES) - {"WON", "LOST"}
LOST_REASONS = frozenset(
    {
        "PRICE_TOO_HIGH",
        "DOWN_PAYMENT_TOO_HIGH",
        "PAYMENT_PLAN_UNSUITABLE",
        "PROPERTY_LOCATION",
        "PROPERTY_TYPE_MISMATCH",
        "PROPERTY_SIZE_MISMATCH",
        "BUYER_NOT_READY",
        "BUYER_PURCHASED_ELSEWHERE",
        "BUYER_UNRESPONSIVE",
        "TRUST_OR_DOCUMENTATION_CONCERN",
        "PROPERTY_UNAVAILABLE",
        "COMPETITOR_SELECTED",
        "DUPLICATE_OPPORTUNITY",
        "OTHER",
        "UNKNOWN",
        "UNCONFIRMED",
    }
)
NON_CALLABLE_LEAD_STATUSES = frozenset(
    {"DO_NOT_CONTACT", "NOT_INTERESTED", "DISQUALIFIED", "CONVERTED"}
)
NEGOTIATION_STATUSES = frozenset(
    {
        "NOT_STARTED",
        "BUYER_OFFER_RECEIVED",
        "SELLER_COUNTEROFFER_RECORDED",
        "NEGOTIATING",
        "PRICE_AGREED",
        "PAYMENT_PLAN_DISCUSSION",
        "AWAITING_DOCUMENTS",
        "AWAITING_BUYER_DECISION",
        "AWAITING_SELLER_DECISION",
        "ACCEPTED_RECORDED",
        "REJECTED",
        "EXPIRED",
        "CANCELLED",
    }
)
RESERVATION_STATUSES = frozenset(
    {"NOT_STARTED", "REQUESTED", "AWAITING_CONFIRMATION", "CONFIRMED", "CANCELLED", "EXPIRED"}
)
PAYMENT_STATUSES = frozenset(
    {"NOT_RECORDED", "PENDING", "PARTIALLY_PAID", "PAID", "REFUNDED", "DISPUTED"}
)
COMMISSION_STATUSES = frozenset(
    {
        "ESTIMATED",
        "CALCULATED",
        "CONFIRMED",
        "PENDING_REVIEW",
        "APPROVED",
        "PARTIALLY_PAID",
        "PAID",
        "CANCELLED",
        "DISPUTED",
    }
)

DEFAULT_PRIORITY_WEIGHTS = {
    "BOOKING_DUE": 100,
    "VISIT_CONFIRMATION": 90,
    "OVERDUE_QUALIFIED": 80,
    "NEGOTIATION_DUE": 70,
    "HIGH_QUALIFIED": 60,
    "OTHER_DUE": 40,
    "NEW_PROSPECT": 20,
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _currency(value: str) -> str:
    currency = str(value).strip().upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise ValueError("Currency must be a three-letter code")
    return currency


def _datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Date and time must include a timezone")
    return parsed.astimezone(UTC)


def contact_is_callable(contact: dict[str, Any]) -> bool:
    """Return whether the current contact record permits an agent-initiated call."""
    return (
        contact.get("contact_eligibility") == "ELIGIBLE"
        and contact.get("lead_status") not in NON_CALLABLE_LEAD_STATUSES
        and contact.get("conversion_state") != "CONVERTED"
        and bool(contact.get("normalized_phone"))
    )


def _record_audit(
    conn: Connection,
    *,
    actor_id: int,
    event_type: str,
    entity_type: str,
    entity_id: UUID | None = None,
    contact_id: UUID | None = None,
    old_value: Any = None,
    new_value: Any = None,
    reason: str | None = None,
    request_key: str | None = None,
) -> None:
    conn.execute(
        """insert into audit_events
             (telegram_user_id,contact_id,event_type,old_value,new_value,entity_type,entity_id,
              actor_telegram_user_id,reason,request_key)
           values (%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s)
           on conflict (request_key,event_type) where request_key is not null do nothing""",
        (
            actor_id,
            contact_id,
            event_type,
            _json(old_value) if old_value is not None else None,
            _json(new_value) if new_value is not None else None,
            entity_type,
            entity_id,
            actor_id,
            reason,
            request_key,
        ),
    )


def _opportunity_event(
    conn: Connection,
    *,
    opportunity_id: UUID,
    event_type: str,
    actor_id: int,
    details: dict[str, Any] | None = None,
    previous_stage: str | None = None,
    new_stage: str | None = None,
    update_id: int | None = None,
) -> None:
    conn.execute(
        """insert into opportunity_events
             (opportunity_id,event_type,previous_stage,new_stage,details,actor_telegram_user_id,telegram_update_id)
           values (%s,%s,%s,%s,%s::jsonb,%s,%s) on conflict (telegram_update_id) do nothing""",
        (
            opportunity_id,
            event_type,
            previous_stage,
            new_stage,
            _json(details or {}),
            actor_id,
            update_id,
        ),
    )


def _visit_event(
    conn: Connection,
    visit_id: UUID,
    event_type: str,
    actor_id: int,
    update_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """insert into property_visit_events
             (visit_id,event_type,details,actor_telegram_user_id,telegram_update_id)
           values (%s,%s,%s::jsonb,%s,%s) on conflict (telegram_update_id) do nothing""",
        (visit_id, event_type, _json(details or {}), actor_id, update_id),
    )


def _negotiation_event(
    conn: Connection,
    negotiation_id: UUID,
    event_type: str,
    actor_id: int,
    update_id: int | None,
    details: dict[str, Any],
) -> None:
    conn.execute(
        """insert into negotiation_events
             (negotiation_id,event_type,details,actor_telegram_user_id,telegram_update_id)
           values (%s,%s,%s::jsonb,%s,%s) on conflict (telegram_update_id) do nothing""",
        (negotiation_id, event_type, _json(details), actor_id, update_id),
    )


def list_stages(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "select stage_code,display_name,sort_order,is_terminal from sales_pipeline_stages where enabled order by sort_order"
    ).fetchall()


def create_opportunity(
    conn: Connection,
    contact_id: UUID,
    actor_id: int,
    update_id: int,
    values: OpportunityInput | dict[str, Any] | None = None,
    *,
    property_id: UUID | None = None,
    campaign_id: UUID | None = None,
) -> dict[str, Any]:
    data = values if isinstance(values, OpportunityInput) else OpportunityInput(**(values or {}))
    contact = conn.execute(
        "select id,conversion_state from contacts where id=%s for share", (contact_id,)
    ).fetchone()
    if not contact:
        raise ValueError("Contact was not found")
    if property_id:
        prop = conn.execute(
            "select id,currency from properties where id=%s", (property_id,)
        ).fetchone()
        if not prop:
            raise ValueError("Property was not found")
        if prop["currency"].strip() != data.currency:
            raise ValueError("Opportunity and property currencies must match")
    row = conn.execute(
        """insert into sales_opportunities
             (contact_id,primary_property_id,campaign_id,current_stage,estimated_sale_value,currency,
              expected_closing_date,next_action,next_action_due,created_by,assigned_telegram_user_id,notes)
           values (%s,%s,%s,'NEW_OPPORTUNITY',%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
        (
            contact_id,
            property_id,
            campaign_id,
            data.estimated_sale_value,
            data.currency,
            data.expected_closing_date,
            data.next_action,
            _datetime(data.next_action_due),
            actor_id,
            actor_id,
            data.notes,
        ),
    ).fetchone()
    _opportunity_event(
        conn,
        opportunity_id=row["id"],
        event_type="CREATED",
        actor_id=actor_id,
        update_id=update_id,
        details={"stage": "NEW_OPPORTUNITY", "property_id": property_id},
    )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="OPPORTUNITY_CREATED",
        entity_type="OPPORTUNITY",
        entity_id=row["id"],
        contact_id=contact_id,
        new_value={"stage": "NEW_OPPORTUNITY", "property_id": property_id},
        request_key=f"{actor_id}:{update_id}" if update_id else None,
    )
    return row


def get_opportunity(conn: Connection, opportunity_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        """select o.*,c.name as contact_name,c.phone_number,c.contact_eligibility,c.lead_status,
                  p.title as property_title,p.availability_status as property_availability,
                  s.score as lead_score,s.priority_category,
                  st.display_name as stage_name,
                  (select max(cl.created_at) from call_logs cl where cl.contact_id=c.id) as last_call_at
           from sales_opportunities o join contacts c on c.id=o.contact_id
           left join properties p on p.id=o.primary_property_id
           left join lead_scores s on s.contact_id=c.id
           join sales_pipeline_stages st on st.stage_code=o.current_stage
           where o.id=%s""",
        (opportunity_id,),
    ).fetchone()


def list_opportunities(
    conn: Connection,
    *,
    stage: str | None = None,
    query: str | None = None,
    page: int = 0,
    page_size: int = 8,
    closed: bool | None = None,
) -> list[dict[str, Any]]:
    if stage is not None and stage not in OPPORTUNITY_STAGES:
        raise ValueError("Unknown pipeline stage")
    page = max(0, min(int(page), 1000))
    page_size = max(1, min(int(page_size), 20))
    term = query.strip()[:100] if query and query.strip() else None
    return conn.execute(
        """select o.id,o.contact_id,o.current_stage,st.display_name as stage_name,
                  o.estimated_sale_value,o.currency,o.expected_closing_date,o.next_action,
                  o.next_action_due,o.last_interaction_at,c.name,c.phone_number,
                  c.contact_eligibility,s.score as lead_score,s.priority_category,
                  p.title as property_title
           from sales_opportunities o join contacts c on c.id=o.contact_id
           join sales_pipeline_stages st on st.stage_code=o.current_stage
           left join lead_scores s on s.contact_id=c.id
           left join properties p on p.id=o.primary_property_id
           where (%s::text is null or o.current_stage=%s)
             and (%s::boolean is null or (o.final_outcome is not null)=%s)
             and (crm_current_user_id() is null or crm_can_access_contact(c.id,crm_current_user_id()))
             and (%s::text is null or c.name ilike %s or c.phone_number ilike %s
                  or coalesce(p.title,'') ilike %s)
           order by (o.next_action_due < now()) desc nulls last,
                    s.score desc nulls last,o.updated_at desc,o.id desc
           limit %s offset %s""",
        (
            stage,
            stage,
            closed,
            closed,
            term,
            f"%{term}%" if term else None,
            f"%{term}%" if term else None,
            f"%{term}%" if term else None,
            page_size,
            page * page_size,
        ),
    ).fetchall()


def opportunity_timeline(
    conn: Connection, opportunity_id: UUID, limit: int = 20
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    return conn.execute(
        """select id,event_type,previous_stage,new_stage,details,actor_telegram_user_id,created_at
           from opportunity_events where opportunity_id=%s order by created_at desc,id desc limit %s""",
        (opportunity_id, limit),
    ).fetchall()


def update_stage(
    conn: Connection,
    opportunity_id: UUID,
    stage: str,
    actor_id: int,
    update_id: int,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    stage = stage.strip().upper()
    if stage not in ACTIVE_STAGES:
        raise ValueError("Won and lost stages require their confirmed close workflows")
    allowed = conn.execute(
        "select stage_code from sales_pipeline_stages where stage_code=%s and enabled", (stage,)
    ).fetchone()
    if not allowed:
        raise ValueError("Pipeline stage is not enabled")
    old = conn.execute(
        "select * from sales_opportunities where id=%s for update", (opportunity_id,)
    ).fetchone()
    if not old:
        raise ValueError("Opportunity was not found")
    if old["final_outcome"] is not None:
        raise ValueError("Closed opportunities must be reopened before changing stage")
    previous_stage = old["current_stage"]
    evidence_queries = {
        "VISIT_SCHEDULED": (
            "select id from property_visits where opportunity_id=%s "
            "and visit_status in ('SCHEDULED','BUYER_ARRIVED') limit 1"
        ),
        "VISIT_COMPLETED": (
            "select id from property_visits where opportunity_id=%s "
            "and visit_status='COMPLETED' limit 1"
        ),
        "NEGOTIATION": "select id from negotiation_records where opportunity_id=%s limit 1",
        "RESERVATION_IN_PROGRESS": (
            "select id from reservations where opportunity_id=%s "
            "and reservation_status not in ('CANCELLED','EXPIRED') limit 1"
        ),
        "BOOKED": (
            "select id from reservations where opportunity_id=%s "
            "and reservation_status='CONFIRMED' limit 1"
        ),
    }
    if (
        stage in evidence_queries
        and not conn.execute(evidence_queries[stage], (opportunity_id,)).fetchone()
    ):
        raise ValueError(f"Record the related {stage.replace('_', ' ').lower()} first")
    if previous_stage == "BOOKED" and stage != "BOOKED":
        reservation = conn.execute(
            "select id from reservations where opportunity_id=%s and reservation_status='CONFIRMED' limit 1",
            (opportunity_id,),
        ).fetchone()
        if reservation:
            raise ValueError("A confirmed reservation must be cancelled before leaving Booked")
    row = conn.execute(
        """update sales_opportunities set current_stage=%s,updated_at=now(),last_interaction_at=now()
           where id=%s returning *""",
        (stage, opportunity_id),
    ).fetchone()
    if previous_stage != stage:
        _opportunity_event(
            conn,
            opportunity_id=opportunity_id,
            event_type="STAGE_CHANGED",
            actor_id=actor_id,
            update_id=update_id,
            details={"reason": (reason or "")[:500]},
            previous_stage=previous_stage,
            new_stage=stage,
        )
        _record_audit(
            conn,
            actor_id=actor_id,
            event_type="OPPORTUNITY_STAGE_CHANGED",
            entity_type="OPPORTUNITY",
            entity_id=opportunity_id,
            contact_id=old["contact_id"],
            old_value={"stage": previous_stage},
            new_value={"stage": stage},
            reason=reason,
            request_key=f"{actor_id}:{update_id}" if update_id else None,
        )
    return row


def add_opportunity_note(
    conn: Connection, opportunity_id: UUID, note: str, actor_id: int, update_id: int
) -> None:
    note = note.strip()
    if not note or len(note) > 4000:
        raise ValueError("Note must contain 1 to 4,000 characters")
    row = conn.execute(
        "update sales_opportunities set last_interaction_at=now(),updated_at=now() where id=%s returning contact_id",
        (opportunity_id,),
    ).fetchone()
    if not row:
        raise ValueError("Opportunity was not found")
    _opportunity_event(
        conn,
        opportunity_id=opportunity_id,
        event_type="NOTE_ADDED",
        actor_id=actor_id,
        update_id=update_id,
        details={"note": note},
    )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="OPPORTUNITY_NOTE_ADDED",
        entity_type="OPPORTUNITY",
        entity_id=opportunity_id,
        contact_id=row["contact_id"],
        request_key=f"{actor_id}:{update_id}" if update_id else None,
    )


def update_next_action(
    conn: Connection,
    opportunity_id: UUID,
    action: str | None,
    due_at: datetime | None,
    actor_id: int,
    update_id: int,
) -> dict[str, Any]:
    if action is not None and (not action.strip() or len(action) > 500):
        raise ValueError("Next action must contain 1 to 500 characters")
    if due_at is not None and (due_at.tzinfo is None or due_at.utcoffset() is None):
        raise ValueError("Next-action time must include a timezone")
    old = conn.execute(
        "select contact_id,next_action,next_action_due from sales_opportunities where id=%s for update",
        (opportunity_id,),
    ).fetchone()
    if not old:
        raise ValueError("Opportunity was not found")
    row = conn.execute(
        """update sales_opportunities set next_action=%s,next_action_due=%s,
             last_interaction_at=now(),updated_at=now() where id=%s returning *""",
        (action.strip() if action else None, _datetime(due_at), opportunity_id),
    ).fetchone()
    _opportunity_event(
        conn,
        opportunity_id=opportunity_id,
        event_type="ACTION_UPDATED",
        actor_id=actor_id,
        update_id=update_id,
        details={"action": action, "due_at": due_at},
    )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="OPPORTUNITY_ACTION_UPDATED",
        entity_type="OPPORTUNITY",
        entity_id=opportunity_id,
        contact_id=old["contact_id"],
        old_value={"action": old["next_action"], "due_at": old["next_action_due"]},
        new_value={"action": action, "due_at": due_at},
        request_key=f"{actor_id}:{update_id}" if update_id else None,
    )
    return row


def close_lost(
    conn: Connection,
    opportunity_id: UUID,
    reason: str,
    explanation: str | None,
    actor_id: int,
    update_id: int,
    *,
    confirmed: bool,
    keep_future_followup: bool = False,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Closing an opportunity requires explicit confirmation")
    reason = reason.strip().upper()
    if reason not in LOST_REASONS:
        raise ValueError("Choose a listed lost reason")
    old = conn.execute(
        "select * from sales_opportunities where id=%s for update", (opportunity_id,)
    ).fetchone()
    if not old:
        raise ValueError("Opportunity was not found")
    if old["final_outcome"] is not None:
        raise ValueError("Opportunity is already closed")
    active_visit = conn.execute(
        """select id from property_visits where opportunity_id=%s
             and visit_status in ('SCHEDULED','BUYER_ARRIVED') limit 1""",
        (opportunity_id,),
    ).fetchone()
    if active_visit:
        raise ValueError("Resolve or cancel active property visits before closing as lost")
    active_reservation = conn.execute(
        """select id from reservations where opportunity_id=%s
             and reservation_status not in ('CANCELLED','EXPIRED') limit 1""",
        (opportunity_id,),
    ).fetchone()
    if active_reservation:
        raise ValueError("Cancel or expire the active reservation before closing as lost")
    row = conn.execute(
        """update sales_opportunities set current_stage='LOST',final_outcome='LOST',
             lost_reason=%s,lost_explanation=%s,future_followup_allowed=%s,
             closed_at=now(),updated_at=now(),last_interaction_at=now()
           where id=%s returning *""",
        (
            reason,
            explanation.strip()[:1000] if explanation else None,
            keep_future_followup,
            opportunity_id,
        ),
    ).fetchone()
    _opportunity_event(
        conn,
        opportunity_id=opportunity_id,
        event_type="LOST",
        actor_id=actor_id,
        update_id=update_id,
        previous_stage=old["current_stage"],
        new_stage="LOST",
        details={
            "reason": reason,
            "explanation": explanation,
            "keep_future_followup": keep_future_followup,
        },
    )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="OPPORTUNITY_CLOSED_LOST",
        entity_type="OPPORTUNITY",
        entity_id=opportunity_id,
        contact_id=old["contact_id"],
        old_value={"stage": old["current_stage"]},
        new_value={"stage": "LOST", "reason": reason, "explanation": explanation},
        reason=explanation,
        request_key=f"{actor_id}:{update_id}" if update_id else None,
    )
    if not keep_future_followup:
        conn.execute(
            """update follow_ups set status='CANCELLED',updated_at=now()
               where opportunity_id=%s and status='PENDING'""",
            (opportunity_id,),
        )
        conn.execute(
            """update contacts set next_follow_up_at=(select min(scheduled_at) from follow_ups
                 where contact_id=%s and status='PENDING'),
                 follow_up_state=case when exists(select 1 from follow_ups where contact_id=%s
                   and status='PENDING' and scheduled_at<now()) then 'OVERDUE'
                   when exists(select 1 from follow_ups where contact_id=%s and status='PENDING')
                   then 'PENDING' else 'NONE' end,updated_at=now() where id=%s""",
            (old["contact_id"], old["contact_id"], old["contact_id"], old["contact_id"]),
        )
    return row


def reopen_lost(
    conn: Connection, opportunity_id: UUID, actor_id: int, update_id: int, *, confirmed: bool
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Reopening a lost opportunity requires confirmation")
    old = conn.execute(
        "select * from sales_opportunities where id=%s for update", (opportunity_id,)
    ).fetchone()
    if not old or old["final_outcome"] != "LOST":
        raise ValueError("Only a lost opportunity can be reopened")
    row = conn.execute(
        """update sales_opportunities set current_stage='NEW_OPPORTUNITY',final_outcome=null,
             lost_reason=null,lost_explanation=null,future_followup_allowed=true,closed_at=null,
             updated_at=now(),last_interaction_at=now()
           where id=%s returning *""",
        (opportunity_id,),
    ).fetchone()
    _opportunity_event(
        conn,
        opportunity_id=opportunity_id,
        event_type="REOPENED",
        actor_id=actor_id,
        update_id=update_id,
        previous_stage="LOST",
        new_stage="NEW_OPPORTUNITY",
    )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="OPPORTUNITY_REOPENED",
        entity_type="OPPORTUNITY",
        entity_id=opportunity_id,
        contact_id=old["contact_id"],
        old_value={"stage": "LOST", "reason": old["lost_reason"]},
        new_value={"stage": "NEW_OPPORTUNITY"},
        request_key=f"{actor_id}:{update_id}" if update_id else None,
    )
    return row


def schedule_opportunity_followup(
    conn: Connection,
    opportunity_id: UUID,
    scheduled_at: datetime,
    followup_type: str,
    actor_id: int,
    *,
    reason: str | None = None,
    notes: str | None = None,
    priority: int = 50,
) -> dict[str, Any]:
    if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
        raise ValueError("Follow-up time must include a timezone")
    if scheduled_at.astimezone(UTC) <= datetime.now(UTC):
        raise ValueError("A follow-up must be scheduled in the future")
    if not 0 <= priority <= 100:
        raise ValueError("Follow-up priority must be between 0 and 100")
    opportunity = conn.execute(
        "select contact_id,campaign_id,current_stage,final_outcome from sales_opportunities where id=%s for update",
        (opportunity_id,),
    ).fetchone()
    if not opportunity:
        raise ValueError("Opportunity was not found")
    if opportunity["final_outcome"]:
        raise ValueError("Cannot schedule a sales follow-up for a closed opportunity")
    contact = conn.execute(
        "select contact_eligibility,lead_status from contacts where id=%s",
        (opportunity["contact_id"],),
    ).fetchone()
    outbound_type = followup_type.strip().upper()
    is_outbound = outbound_type.startswith("CUSTOM:") or outbound_type in {
        "CALL_BACK",
        "SEND_PROPERTY_INFORMATION",
        "SEND_PAYMENT_PLAN",
        "ARRANGE_PROPERTY_VISIT",
        "CONFIRM_PROPERTY_VISIT",
        "FOLLOW_UP_AFTER_VISIT",
        "NEGOTIATION_FOLLOW_UP",
        "BOOKING_FOLLOW_UP",
        "GENERAL_FOLLOW_UP",
    }
    if (
        contact
        and (
            contact["contact_eligibility"] == "DO_NOT_CONTACT"
            or contact["lead_status"] == "DO_NOT_CONTACT"
        )
        and is_outbound
    ):
        raise ValueError("This contact is marked Do Not Contact")
    created = conn.execute(
        """insert into follow_ups
             (contact_id,campaign_id,opportunity_id,follow_up_type,scheduled_at,status,notes,reason,priority)
           values (%s,%s,%s,%s,%s,'PENDING',%s,%s,%s) returning *""",
        (
            opportunity["contact_id"],
            opportunity["campaign_id"],
            opportunity_id,
            followup_type.strip().upper()[:50],
            scheduled_at.astimezone(UTC),
            notes[:2000] if notes else None,
            reason[:500] if reason else None,
            priority,
        ),
    ).fetchone()
    conn.execute(
        "insert into reminder_deliveries (follow_up_id,telegram_user_id) values (%s,%s) on conflict do nothing",
        (created["id"], actor_id),
    )
    conn.execute(
        """update sales_opportunities set next_action=%s,next_action_due=%s,
             updated_at=now(),last_interaction_at=now() where id=%s""",
        (followup_type.replace("_", " ").title(), scheduled_at.astimezone(UTC), opportunity_id),
    )
    conn.execute(
        """update contacts set next_follow_up_at=(select min(scheduled_at) from follow_ups
             where contact_id=%s and status='PENDING'),
             follow_up_state=case when exists(select 1 from follow_ups where contact_id=%s
               and status='PENDING' and scheduled_at<now()) then 'OVERDUE'
               when exists(select 1 from follow_ups where contact_id=%s and status='PENDING')
               then 'PENDING' else 'NONE' end,updated_at=now() where id=%s""",
        (
            opportunity["contact_id"],
            opportunity["contact_id"],
            opportunity["contact_id"],
            opportunity["contact_id"],
        ),
    )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="FOLLOW_UP_CREATED",
        entity_type="FOLLOW_UP",
        entity_id=created["id"],
        contact_id=opportunity["contact_id"],
        new_value={
            "opportunity_id": opportunity_id,
            "type": followup_type,
            "scheduled_at": scheduled_at,
        },
    )
    return created


def schedule_contact_followup(
    conn: Connection,
    contact_id: UUID,
    scheduled_at: datetime,
    followup_type: str,
    actor_id: int,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
        raise ValueError("Follow-up time must include a timezone")
    scheduled_at = scheduled_at.astimezone(UTC)
    if scheduled_at <= datetime.now(UTC):
        raise ValueError("A follow-up must be scheduled in the future")
    contact = conn.execute(
        """select campaign_id,contact_eligibility,lead_status,conversion_state,normalized_phone
           from contacts
           where id=%s for update""",
        (contact_id,),
    ).fetchone()
    if not contact:
        raise ValueError("Contact was not found")
    if not contact_is_callable(contact):
        raise ValueError("This contact is not eligible for an outbound follow-up")
    created = conn.execute(
        """insert into follow_ups
             (contact_id,campaign_id,follow_up_type,scheduled_at,status,notes,reason)
           values (%s,%s,%s,%s,'PENDING',%s,%s) returning *""",
        (
            contact_id,
            contact["campaign_id"],
            followup_type.strip().upper()[:50],
            scheduled_at,
            reason[:2000] if reason else None,
            reason[:500] if reason else None,
        ),
    ).fetchone()
    conn.execute(
        """insert into reminder_deliveries (follow_up_id,telegram_user_id)
           values (%s,%s) on conflict do nothing""",
        (created["id"], actor_id),
    )
    conn.execute(
        """update contacts set next_follow_up_at=(select min(scheduled_at) from follow_ups
             where contact_id=%s and status='PENDING'),
             follow_up_state=case when exists(select 1 from follow_ups where contact_id=%s
               and status='PENDING' and scheduled_at<now()) then 'OVERDUE'
               when exists(select 1 from follow_ups where contact_id=%s and status='PENDING')
               then 'PENDING' else 'NONE' end,updated_at=now() where id=%s""",
        (contact_id, contact_id, contact_id, contact_id),
    )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="FOLLOW_UP_CREATED",
        entity_type="FOLLOW_UP",
        entity_id=created["id"],
        contact_id=contact_id,
        new_value={"type": followup_type, "scheduled_at": scheduled_at, "reason": reason},
    )
    return created


def reschedule_followup(
    conn: Connection,
    followup_id: UUID,
    scheduled_at: datetime,
    actor_id: int,
    update_id: int,
) -> dict[str, Any]:
    if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
        raise ValueError("Follow-up time must include a timezone")
    scheduled_at = scheduled_at.astimezone(UTC)
    if scheduled_at <= datetime.now(UTC):
        raise ValueError("A follow-up must be rescheduled into the future")
    old = conn.execute("select * from follow_ups where id=%s for update", (followup_id,)).fetchone()
    if not old or old["status"] != "PENDING":
        raise ValueError("Follow-up is no longer pending")
    if old.get("opportunity_id"):
        opportunity = conn.execute(
            "select final_outcome from sales_opportunities where id=%s for update",
            (old["opportunity_id"],),
        ).fetchone()
        if not opportunity or opportunity["final_outcome"] is not None:
            raise ValueError("Cannot reschedule a follow-up for a closed opportunity")
    contact = conn.execute(
        "select contact_eligibility,lead_status from contacts where id=%s",
        (old["contact_id"],),
    ).fetchone()
    is_outbound = old["follow_up_type"].upper().startswith("CUSTOM:") or old["follow_up_type"] in {
        "CALL_BACK",
        "SEND_PROPERTY_INFORMATION",
        "SEND_PAYMENT_PLAN",
        "ARRANGE_PROPERTY_VISIT",
        "CONFIRM_PROPERTY_VISIT",
        "FOLLOW_UP_AFTER_VISIT",
        "NEGOTIATION_FOLLOW_UP",
        "BOOKING_FOLLOW_UP",
        "GENERAL_FOLLOW_UP",
    }
    if (
        contact
        and (
            contact["contact_eligibility"] == "DO_NOT_CONTACT"
            or contact["lead_status"] == "DO_NOT_CONTACT"
        )
        and is_outbound
    ):
        raise ValueError("This contact is marked Do Not Contact")
    with conn.transaction():
        conn.execute(
            "update follow_ups set status='RESCHEDULED',updated_at=now() where id=%s",
            (followup_id,),
        )
        created = conn.execute(
            """insert into follow_ups
                 (contact_id,campaign_id,opportunity_id,follow_up_type,scheduled_at,status,notes,reason,priority)
               values (%s,%s,%s,%s,%s,'PENDING',%s,%s,%s) returning *""",
            (
                old["contact_id"],
                old["campaign_id"],
                old["opportunity_id"],
                old["follow_up_type"],
                scheduled_at,
                old["notes"],
                old.get("reason"),
                old["priority"],
            ),
        ).fetchone()
        conn.execute(
            "update follow_ups set rescheduled_to=%s where id=%s",
            (created["id"], followup_id),
        )
        conn.execute(
            "insert into reminder_deliveries (follow_up_id,telegram_user_id) values (%s,%s) on conflict do nothing",
            (created["id"], actor_id),
        )
        conn.execute(
            """update contacts set next_follow_up_at=(select min(scheduled_at) from follow_ups
                 where contact_id=%s and status='PENDING'),
                 follow_up_state=case when exists(select 1 from follow_ups where contact_id=%s
                   and status='PENDING' and scheduled_at<now()) then 'OVERDUE'
                   when exists(select 1 from follow_ups where contact_id=%s and status='PENDING')
                   then 'PENDING' else 'NONE' end,updated_at=now() where id=%s""",
            (old["contact_id"], old["contact_id"], old["contact_id"], old["contact_id"]),
        )
        if old.get("opportunity_id"):
            conn.execute(
                """update sales_opportunities set next_action=%s,next_action_due=%s,
                     updated_at=now(),last_interaction_at=now() where id=%s""",
                (
                    old["follow_up_type"].replace("_", " ").title(),
                    scheduled_at,
                    old["opportunity_id"],
                ),
            )
        _record_audit(
            conn,
            actor_id=actor_id,
            event_type="FOLLOW_UP_RESCHEDULED",
            entity_type="FOLLOW_UP",
            entity_id=followup_id,
            contact_id=old["contact_id"],
            old_value={"scheduled_at": old["scheduled_at"], "status": "PENDING"},
            new_value={
                "scheduled_at": scheduled_at,
                "status": "RESCHEDULED",
                "replacement_id": created["id"],
            },
            request_key=f"{actor_id}:{update_id}" if update_id else None,
        )
    return created


def list_stage_opportunities(
    conn: Connection, stage: str, page: int = 0, page_size: int = 8
) -> list[dict[str, Any]]:
    return list_opportunities(conn, stage=stage, page=page, page_size=page_size, closed=False)


def create_visit(
    conn: Connection,
    opportunity_id: UUID,
    property_id: UUID,
    values: VisitInput | dict[str, Any],
    actor_id: int,
    update_id: int,
) -> dict[str, Any]:
    data = values if isinstance(values, VisitInput) else VisitInput(**values)
    scheduled_at = datetime.fromisoformat(data.scheduled_at).astimezone(UTC)
    if scheduled_at <= datetime.now(UTC):
        raise ValueError("A property visit must be scheduled in the future")
    row = conn.execute(
        """select o.contact_id,o.primary_property_id,o.current_stage,o.final_outcome,
                  c.contact_eligibility,c.lead_status,
                  p.currency,p.availability_status from sales_opportunities o
           join contacts c on c.id=o.contact_id join properties p on p.id=%s
           where o.id=%s for update of o""",
        (property_id, opportunity_id),
    ).fetchone()
    if not row:
        raise ValueError("Opportunity or property was not found")
    if row["final_outcome"]:
        raise ValueError("A closed opportunity cannot have a new visit")
    if row["contact_eligibility"] == "DO_NOT_CONTACT" or row["lead_status"] == "DO_NOT_CONTACT":
        raise ValueError("This contact is marked Do Not Contact")
    if row["availability_status"] != "AVAILABLE" and not data.availability_confirmed:
        raise ValueError("Confirm with the seller that this property can be visited")
    duplicate = conn.execute(
        """select id from property_visits where opportunity_id=%s and scheduled_at=%s
             and visit_status in ('SCHEDULED','BUYER_ARRIVED') limit 1""",
        (opportunity_id, scheduled_at),
    ).fetchone()
    if duplicate and not data.duplicate_confirmed:
        raise ValueError(
            "An active visit already exists at that time; confirm an intentional duplicate"
        )
    visit = conn.execute(
        """insert into property_visits
             (opportunity_id,contact_id,property_id,scheduled_at,meeting_point,internal_note,
              buyer_instructions,duplicate_confirmed,created_by)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
        (
            opportunity_id,
            row["contact_id"],
            property_id,
            scheduled_at,
            data.meeting_point,
            data.internal_note,
            data.buyer_instructions,
            data.duplicate_confirmed,
            actor_id,
        ),
    ).fetchone()
    _visit_event(
        conn,
        visit["id"],
        "SCHEDULED",
        actor_id,
        update_id,
        {
            "scheduled_at": scheduled_at,
            "property_id": property_id,
            "confirmation_status": "NOT_CONFIRMED",
        },
    )
    conn.execute(
        """update sales_opportunities set primary_property_id=%s,current_stage='VISIT_SCHEDULED',
             next_action='Confirm property visit',next_action_due=%s,last_interaction_at=now(),updated_at=now()
           where id=%s""",
        (property_id, scheduled_at - timedelta(hours=24), opportunity_id),
    )
    if row["current_stage"] != "VISIT_SCHEDULED":
        _opportunity_event(
            conn,
            opportunity_id=opportunity_id,
            event_type="STAGE_CHANGED",
            actor_id=actor_id,
            previous_stage=row["current_stage"],
            new_stage="VISIT_SCHEDULED",
            details={"visit_id": visit["id"]},
        )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="PROPERTY_VISIT_SCHEDULED",
        entity_type="PROPERTY_VISIT",
        entity_id=visit["id"],
        contact_id=row["contact_id"],
        new_value={
            "opportunity_id": opportunity_id,
            "property_id": property_id,
            "scheduled_at": scheduled_at,
        },
        request_key=f"{actor_id}:{update_id}" if update_id else None,
    )
    return visit


def get_visit(conn: Connection, visit_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        """select v.*,c.name as contact_name,c.phone_number,p.title as property_title,
                  p.availability_status as property_availability,
                  o.current_stage,o.id as opportunity_id
           from property_visits v join contacts c on c.id=v.contact_id
           join properties p on p.id=v.property_id join sales_opportunities o on o.id=v.opportunity_id
           where v.id=%s""",
        (visit_id,),
    ).fetchone()


def update_visit(
    conn: Connection,
    visit_id: UUID,
    action: str,
    actor_id: int,
    update_id: int,
    *,
    confirmed: bool = False,
    feedback: dict[str, Any] | None = None,
    buyer_liked_property: bool | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    actions = {
        "CONFIRM",
        "ARRIVED",
        "COMPLETE",
        "NO_SHOW",
        "CANCEL",
        "RESCHEDULE",
        "RESCHEDULE_REQUESTED",
        "FEEDBACK",
    }
    action = action.upper()
    if action not in actions:
        raise ValueError("Unknown visit action")
    if action in {"CANCEL", "COMPLETE", "NO_SHOW"} and not confirmed:
        raise ValueError("Confirm this visit update before saving")
    old = conn.execute(
        "select * from property_visits where id=%s for update", (visit_id,)
    ).fetchone()
    if not old:
        raise ValueError("Visit was not found")
    if (
        old["visit_status"] in {"CANCELLED", "RESCHEDULED", "COMPLETED", "DID_NOT_ATTEND"}
        and action != "FEEDBACK"
    ):
        raise ValueError("This visit is already resolved")
    if action == "FEEDBACK" and old["visit_status"] not in {
        "COMPLETED",
        "DID_NOT_ATTEND",
    }:
        raise ValueError("Record attendance or a no-show before adding visit feedback")
    fields: dict[str, Any] = {"updated_at": "now()"}
    event_details: dict[str, Any] = {}
    if action == "CONFIRM":
        fields = {"confirmation_status": "CONFIRMED", "updated_at": "now()"}
    elif action == "RESCHEDULE":
        fields = {
            "visit_status": "RESCHEDULED",
            "confirmation_status": "RESCHEDULE_REQUESTED",
            "updated_at": "now()",
        }
    elif action == "ARRIVED":
        fields = {"visit_status": "BUYER_ARRIVED", "updated_at": "now()"}
    elif action == "COMPLETE":
        fields = {
            "visit_status": "COMPLETED",
            "attendance_outcome": "ATTENDED",
            "completed_at": "now()",
            "updated_at": "now()",
        }
    elif action == "NO_SHOW":
        fields = {
            "visit_status": "DID_NOT_ATTEND",
            "attendance_outcome": "DID_NOT_ATTEND",
            "completed_at": "now()",
            "updated_at": "now()",
        }
    elif action == "CANCEL":
        fields = {
            "visit_status": "CANCELLED",
            "confirmation_status": "CANCELLED",
            "updated_at": "now()",
        }
    elif action == "RESCHEDULE_REQUESTED":
        fields = {"confirmation_status": "RESCHEDULE_REQUESTED", "updated_at": "now()"}
    else:
        clean_feedback = feedback or {}
        allowed = {
            "outcome",
            "liked",
            "likes",
            "concerns",
            "notes",
            "offer_interest",
            "needs_alternatives",
            "next_action",
        }
        if set(clean_feedback) - allowed:
            raise ValueError("Feedback contains unsupported fields")
        outcome = clean_feedback.get("outcome")
        allowed_outcomes = {
            "STRONG_INTEREST",
            "INTERESTED_WITH_CONCERNS",
            "WANTS_ALTERNATIVES",
            "NEEDS_FAMILY_DISCUSSION",
            "PRICE_NEGOTIATION",
            "NOT_INTERESTED",
            "DID_NOT_ATTEND",
            "RESCHEDULE_REQUIRED",
            "OTHER",
        }
        if outcome not in allowed_outcomes:
            raise ValueError("Choose a supported visit feedback outcome")
        event_details = {**clean_feedback, "buyer_liked_property": buyer_liked_property}
        fields = {
            "feedback": json.dumps(clean_feedback, ensure_ascii=False),
            "buyer_liked_property": buyer_liked_property,
            "next_action": next_action[:500] if next_action else clean_feedback.get("next_action"),
            "updated_at": "now()",
        }
    set_sql = ",".join(
        f"{field}=%s::jsonb"
        if field == "feedback"
        else f"{field}=%s"
        if value != "now()"
        else f"{field}=now()"
        for field, value in fields.items()
    )
    params = [value for value in fields.values() if value != "now()"]
    params.append(visit_id)
    row = conn.execute(
        f"update property_visits set {set_sql} where id=%s returning *", params
    ).fetchone()
    event_type = {
        "CONFIRM": "CONFIRMED",
        "ARRIVED": "BUYER_ARRIVED",
        "COMPLETE": "COMPLETED",
        "NO_SHOW": "DID_NOT_ATTEND",
        "CANCEL": "CANCELLED",
        "RESCHEDULE": "RESCHEDULED",
        "RESCHEDULE_REQUESTED": "RESCHEDULE_REQUESTED",
        "FEEDBACK": "FEEDBACK_RECORDED",
    }[action]
    _visit_event(conn, visit_id, event_type, actor_id, update_id, event_details)
    _opportunity_event(
        conn,
        opportunity_id=old["opportunity_id"],
        event_type="VISIT_UPDATED",
        actor_id=actor_id,
        details={
            "visit_id": visit_id,
            "visit_event": event_type,
            "previous_status": old["visit_status"],
            "new_status": row["visit_status"],
            **event_details,
        },
    )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type=f"PROPERTY_VISIT_{event_type}",
        entity_type="PROPERTY_VISIT",
        entity_id=visit_id,
        contact_id=old["contact_id"],
        old_value={
            "visit_status": old["visit_status"],
            "confirmation_status": old["confirmation_status"],
        },
        new_value={
            "visit_status": row["visit_status"],
            "confirmation_status": row["confirmation_status"],
            **event_details,
        },
        request_key=f"{actor_id}:{update_id}" if update_id else None,
    )
    if action in {"COMPLETE", "NO_SHOW"}:
        stage = "VISIT_COMPLETED" if action == "COMPLETE" else "FOLLOW_UP"
        opportunity = conn.execute(
            "select current_stage,final_outcome from sales_opportunities where id=%s for update",
            (old["opportunity_id"],),
        ).fetchone()
        if (
            opportunity
            and opportunity["final_outcome"] is None
            and opportunity["current_stage"] == "VISIT_SCHEDULED"
        ):
            conn.execute(
                """update sales_opportunities set current_stage=%s,next_action=%s,
                     last_interaction_at=now(),updated_at=now() where id=%s""",
                (
                    stage,
                    "Record visit feedback"
                    if action == "COMPLETE"
                    else "Reschedule or follow up after missed visit",
                    old["opportunity_id"],
                ),
            )
            _opportunity_event(
                conn,
                opportunity_id=old["opportunity_id"],
                event_type="STAGE_CHANGED",
                actor_id=actor_id,
                previous_stage="VISIT_SCHEDULED",
                new_stage=stage,
                details={"visit_id": visit_id, "attendance": row["attendance_outcome"]},
            )
    if action == "FEEDBACK":
        conn.execute(
            "update sales_opportunities set next_action=%s,last_interaction_at=now(),updated_at=now() where id=%s and final_outcome is null",
            (row.get("next_action"), old["opportunity_id"]),
        )
    return row


def visit_timeline(conn: Connection, visit_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
    return conn.execute(
        "select * from property_visit_events where visit_id=%s order by created_at,id limit %s",
        (visit_id, max(1, min(limit, 100))),
    ).fetchall()


def calculate_price_difference(
    asking_price: Decimal | int | str | None,
    buyer_offer: Decimal | int | str | None,
    asking_currency: str | None,
    offer_currency: str | None,
) -> dict[str, Any]:
    if asking_price is None or buyer_offer is None:
        return {"difference": None, "percentage_difference": None, "currency": None}
    if (
        not asking_currency
        or not offer_currency
        or _currency(asking_currency) != _currency(offer_currency)
    ):
        raise ValueError("Cannot compare prices in different or unknown currencies")
    asking = Decimal(str(asking_price))
    offer = Decimal(str(buyer_offer))
    difference = asking - offer
    percentage = (difference * Decimal(100) / asking) if asking != 0 else None
    return {
        "difference": difference.quantize(Decimal("0.01")),
        "percentage_difference": percentage.quantize(Decimal("0.01"))
        if percentage is not None
        else None,
        "currency": _currency(asking_currency),
    }


def save_negotiation(
    conn: Connection,
    opportunity_id: UUID,
    values: NegotiationInput | dict[str, Any],
    actor_id: int,
    update_id: int,
) -> dict[str, Any]:
    data = values if isinstance(values, NegotiationInput) else NegotiationInput(**values)
    currency = _currency(data.currency)
    opportunity = conn.execute(
        """select o.contact_id,o.primary_property_id,o.currency,o.current_stage,o.final_outcome,
                  p.currency as property_currency,p.price as property_price,p.title as property_title
           from sales_opportunities o left join properties p on p.id=o.primary_property_id
           where o.id=%s for update of o""",
        (opportunity_id,),
    ).fetchone()
    if not opportunity:
        raise ValueError("Opportunity was not found")
    if opportunity["final_outcome"]:
        raise ValueError("Cannot update a negotiation on a closed opportunity")
    property_id = opportunity["primary_property_id"]
    if property_id and opportunity["property_currency"].strip() != currency:
        raise ValueError("Negotiation currency must match the recorded property currency")
    if data.negotiation_status and data.negotiation_status not in NEGOTIATION_STATUSES:
        raise ValueError("Choose a supported negotiation status")
    existing = conn.execute(
        "select * from negotiation_records where opportunity_id=%s for update", (opportunity_id,)
    ).fetchone()
    submitted = data.model_dump(exclude_unset=True)
    if data.asking_price is None and existing is None and opportunity["property_price"] is not None:
        submitted["asking_price"] = opportunity["property_price"]
    if existing:
        new = dict(existing)
        for key, value in submitted.items():
            if (
                key
                in {
                    "asking_price",
                    "buyer_offer",
                    "seller_counteroffer",
                    "current_discussed_price",
                    "currency",
                    "down_payment",
                    "payment_plan",
                    "proposed_reservation_amount",
                    "offer_date",
                    "offer_expiry_date",
                    "negotiation_status",
                    "next_action",
                    "next_action_due",
                }
                and value is not None
            ):
                new[key] = (
                    _datetime(value) if key == "next_action_due" and value is not None else value
                )
        if new.get("current_discussed_price") is not None and new.get("down_payment") is not None:
            if Decimal(str(new["down_payment"])) > Decimal(str(new["current_discussed_price"])):
                raise ValueError("Down payment cannot exceed the current discussed price")
        assignments = (
            "asking_price",
            "buyer_offer",
            "seller_counteroffer",
            "current_discussed_price",
            "currency",
            "down_payment",
            "payment_plan",
            "proposed_reservation_amount",
            "offer_date",
            "offer_expiry_date",
            "negotiation_status",
            "next_action",
            "next_action_due",
        )
        row = conn.execute(
            """update negotiation_records set asking_price=%s,buyer_offer=%s,seller_counteroffer=%s,
                 current_discussed_price=%s,currency=%s,down_payment=%s,payment_plan=%s,
                 proposed_reservation_amount=%s,offer_date=%s,offer_expiry_date=%s,
                 negotiation_status=%s,next_action=%s,next_action_due=%s,updated_by=%s,updated_at=now()
               where id=%s returning *""",
            tuple(new.get(k) for k in assignments) + (actor_id, existing["id"]),
        ).fetchone()
        negotiation_id = row["id"]
        old_values = {key: existing.get(key) for key in assignments}
    else:
        status = data.negotiation_status or "NOT_STARTED"
        row = conn.execute(
            """insert into negotiation_records
                 (opportunity_id,property_id,asking_price,buyer_offer,seller_counteroffer,
                  current_discussed_price,currency,down_payment,payment_plan,proposed_reservation_amount,
                  offer_date,offer_expiry_date,negotiation_status,next_action,next_action_due,updated_by)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
            (
                opportunity_id,
                property_id,
                submitted.get("asking_price"),
                data.buyer_offer,
                data.seller_counteroffer,
                data.current_discussed_price,
                currency,
                data.down_payment,
                data.payment_plan,
                data.proposed_reservation_amount,
                data.offer_date,
                data.offer_expiry_date,
                status,
                data.next_action,
                _datetime(data.next_action_due),
                actor_id,
            ),
        ).fetchone()
        negotiation_id = row["id"]
        old_values = {}
    if data.buyer_offer is not None:
        event_type = data.event_type or "INITIAL_OFFER"
    elif data.seller_counteroffer is not None:
        event_type = data.event_type or "COUNTEROFFER"
    elif data.note:
        event_type = data.event_type or "NOTE_ADDED"
    elif data.payment_plan is not None:
        event_type = data.event_type or "PAYMENT_PLAN_DISCUSSION"
    else:
        event_type = data.event_type or "PRICE_DISCUSSION"
    if event_type not in {
        "INITIAL_OFFER",
        "COUNTEROFFER",
        "PRICE_DISCUSSION",
        "PAYMENT_PLAN_DISCUSSION",
        "BUYER_RESPONSE",
        "SELLER_RESPONSE",
        "DOCUMENTS_REQUESTED",
        "NEXT_ACTION",
        "STATUS_CHANGED",
        "FINAL_RESULT",
        "NOTE_ADDED",
    }:
        raise ValueError("Choose a supported negotiation event")
    details = {
        "recorded_fields": {key: value for key, value in submitted.items() if key != "note"},
        "previous_values": old_values,
        "note": data.note,
    }
    _negotiation_event(conn, negotiation_id, event_type, actor_id, update_id, details)
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="NEGOTIATION_UPDATED",
        entity_type="NEGOTIATION",
        entity_id=negotiation_id,
        contact_id=opportunity["contact_id"],
        old_value=old_values,
        new_value=details["recorded_fields"],
        request_key=f"{actor_id}:{update_id}" if update_id else None,
    )
    if opportunity["primary_property_id"] is None and property_id:
        conn.execute(
            "update sales_opportunities set primary_property_id=%s where id=%s",
            (property_id, opportunity_id),
        )
    if opportunity["current_stage"] != "NEGOTIATION":
        conn.execute(
            """update sales_opportunities set current_stage='NEGOTIATION',
                 next_action=coalesce(%s,next_action),next_action_due=coalesce(%s,next_action_due),
                 last_interaction_at=now(),updated_at=now() where id=%s""",
            (data.next_action, _datetime(data.next_action_due), opportunity_id),
        )
        _opportunity_event(
            conn,
            opportunity_id=opportunity_id,
            event_type="STAGE_CHANGED",
            actor_id=actor_id,
            previous_stage=opportunity["current_stage"],
            new_stage="NEGOTIATION",
            details={"negotiation_id": negotiation_id, "event_type": event_type},
        )
    else:
        conn.execute(
            """update sales_opportunities set next_action=coalesce(%s,next_action),
                 next_action_due=coalesce(%s,next_action_due),last_interaction_at=now(),updated_at=now()
               where id=%s""",
            (data.next_action, _datetime(data.next_action_due), opportunity_id),
        )
        _opportunity_event(
            conn,
            opportunity_id=opportunity_id,
            event_type="NEGOTIATION_UPDATED",
            actor_id=actor_id,
            details={"negotiation_id": negotiation_id, "event_type": event_type},
        )
    return row


def negotiation_timeline(
    conn: Connection, negotiation_id: UUID, limit: int = 50
) -> list[dict[str, Any]]:
    return conn.execute(
        "select * from negotiation_events where negotiation_id=%s order by created_at,id limit %s",
        (negotiation_id, max(1, min(limit, 100))),
    ).fetchall()


def create_reservation(
    conn: Connection,
    opportunity_id: UUID,
    property_id: UUID,
    values: ReservationInput | dict[str, Any],
    actor_id: int,
    update_id: int,
    *,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Recording a reservation requires confirmation")
    data = values if isinstance(values, ReservationInput) else ReservationInput(**values)
    currency = _currency(data.currency)
    opportunity = conn.execute(
        """select contact_id,current_stage,final_outcome from sales_opportunities where id=%s for update""",
        (opportunity_id,),
    ).fetchone()
    if not opportunity:
        raise ValueError("Opportunity was not found")
    if opportunity["final_outcome"]:
        raise ValueError("Closed opportunities cannot have new reservations")
    active = conn.execute(
        """select id from reservations where opportunity_id=%s
             and reservation_status not in ('CANCELLED','EXPIRED') limit 1""",
        (opportunity_id,),
    ).fetchone()
    if active:
        raise ValueError("An active reservation already exists for this opportunity")
    prop = conn.execute("select currency from properties where id=%s", (property_id,)).fetchone()
    if not prop or prop["currency"].strip() != currency:
        raise ValueError("Reservation currency must match the property currency")
    row = conn.execute(
        """insert into reservations
             (opportunity_id,property_id,reservation_date,reservation_amount,currency,
              reservation_reference,expires_at,notes,created_by)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
        (
            opportunity_id,
            property_id,
            data.reservation_date,
            data.reservation_amount,
            currency,
            data.reservation_reference,
            _datetime(data.expires_at),
            data.notes,
            actor_id,
        ),
    ).fetchone()
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="RESERVATION_RECORDED",
        entity_type="RESERVATION",
        entity_id=row["id"],
        contact_id=opportunity["contact_id"],
        new_value={"status": "REQUESTED", "amount": data.reservation_amount, "currency": currency},
        request_key=f"{actor_id}:{update_id}" if update_id else None,
    )
    if opportunity["current_stage"] != "RESERVATION_IN_PROGRESS":
        _opportunity_event(
            conn,
            opportunity_id=opportunity_id,
            event_type="STAGE_CHANGED",
            actor_id=actor_id,
            update_id=update_id,
            previous_stage=opportunity["current_stage"],
            details={"reservation_id": row["id"]},
            new_stage="RESERVATION_IN_PROGRESS",
        )
    else:
        _opportunity_event(
            conn,
            opportunity_id=opportunity_id,
            event_type="RESERVATION_UPDATED",
            actor_id=actor_id,
            details={"reservation_id": row["id"], "status": "REQUESTED"},
        )
    conn.execute(
        "update sales_opportunities set current_stage='RESERVATION_IN_PROGRESS',primary_property_id=%s,updated_at=now(),last_interaction_at=now() where id=%s",
        (property_id, opportunity_id),
    )
    return row


def update_reservation(
    conn: Connection,
    reservation_id: UUID,
    *,
    status: str | None = None,
    payment_status: str | None = None,
    actor_id: int,
    confirmed: bool,
    update_id: int | None = None,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Reservation changes require confirmation")
    if status is not None and status not in RESERVATION_STATUSES:
        raise ValueError("Choose a supported reservation status")
    if payment_status is not None and payment_status not in PAYMENT_STATUSES:
        raise ValueError("Choose a supported payment status")
    old = conn.execute(
        "select * from reservations where id=%s for update", (reservation_id,)
    ).fetchone()
    if not old:
        raise ValueError("Reservation was not found")
    if old["reservation_status"] in {"CANCELLED", "EXPIRED"}:
        raise ValueError("Cancelled or expired reservations cannot be changed")
    opportunity = conn.execute(
        "select contact_id,current_stage,final_outcome from sales_opportunities where id=%s for update",
        (old["opportunity_id"],),
    ).fetchone()
    if not opportunity:
        raise ValueError("Reservation opportunity was not found")
    if opportunity["final_outcome"] is not None:
        raise ValueError("Reservations on closed opportunities cannot be changed")
    row = conn.execute(
        """update reservations set reservation_status=coalesce(%s,reservation_status),
             payment_status=coalesce(%s,payment_status),updated_at=now() where id=%s returning *""",
        (status, payment_status, reservation_id),
    ).fetchone()
    resulting_status = status or old["reservation_status"]
    if resulting_status == "CONFIRMED":
        next_stage = "BOOKED"
    elif resulting_status in {"REQUESTED", "AWAITING_CONFIRMATION"}:
        next_stage = "RESERVATION_IN_PROGRESS"
    elif resulting_status == "NOT_STARTED":
        next_stage = "NEGOTIATION"
    elif resulting_status in {"CANCELLED", "EXPIRED"}:
        has_other_reservation = conn.execute(
            """select 1 from reservations where opportunity_id=%s and id<>%s
                 and reservation_status not in ('CANCELLED','EXPIRED') limit 1""",
            (old["opportunity_id"], reservation_id),
        ).fetchone()
        if has_other_reservation:
            next_stage = "RESERVATION_IN_PROGRESS"
        else:
            has_negotiation = conn.execute(
                "select 1 from negotiation_records where opportunity_id=%s limit 1",
                (old["opportunity_id"],),
            ).fetchone()
            next_stage = "NEGOTIATION" if has_negotiation else "FOLLOW_UP"
    else:
        next_stage = opportunity["current_stage"]
    if next_stage != opportunity["current_stage"]:
        conn.execute(
            "update sales_opportunities set current_stage=%s,updated_at=now(),last_interaction_at=now() where id=%s",
            (next_stage, old["opportunity_id"]),
        )
        _opportunity_event(
            conn,
            opportunity_id=old["opportunity_id"],
            event_type="STAGE_CHANGED",
            actor_id=actor_id,
            previous_stage=opportunity["current_stage"],
            new_stage=next_stage,
            details={"reservation_id": reservation_id, "status": row["reservation_status"]},
        )
    _opportunity_event(
        conn,
        opportunity_id=old["opportunity_id"],
        event_type="RESERVATION_UPDATED",
        actor_id=actor_id,
        details={
            "reservation_id": reservation_id,
            "old_status": old["reservation_status"],
            "new_status": row["reservation_status"],
            "old_payment_status": old["payment_status"],
            "new_payment_status": row["payment_status"],
        },
    )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="RESERVATION_STATUS_CHANGED",
        entity_type="RESERVATION",
        entity_id=reservation_id,
        old_value={
            "reservation_status": old["reservation_status"],
            "payment_status": old["payment_status"],
        },
        new_value={
            "reservation_status": row["reservation_status"],
            "payment_status": row["payment_status"],
        },
        request_key=f"{actor_id}:{update_id}" if update_id else None,
    )
    return row


def _commission_rule(
    conn: Connection, *, currency: str, project_name: str | None, agent_id: int, sale_date: date
) -> dict[str, Any] | None:
    return conn.execute(
        """select * from commission_rules where active and currency=%s
             and (project_name is null or project_name=%s)
             and (agent_telegram_user_id is null or agent_telegram_user_id=%s)
             and (effective_from is null or effective_from<=%s)
             and (effective_to is null or effective_to>=%s)
           order by (project_name is not null) desc,(agent_telegram_user_id is not null) desc,
                    effective_from desc nulls last,created_at desc limit 1""",
        (currency, project_name, agent_id, sale_date, sale_date),
    ).fetchone()


def commission_amount(
    rule_type: str, amount_or_rate: Decimal | str | int, sale_price: Decimal | str | int
) -> Decimal:
    base = Decimal(str(sale_price))
    parameter = Decimal(str(amount_or_rate))
    if rule_type == "PERCENTAGE":
        amount = base * parameter / Decimal(100)
    elif rule_type == "FIXED":
        amount = parameter
    else:
        raise ValueError("Commission rule must be FIXED or PERCENTAGE")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def record_sale(
    conn: Connection,
    opportunity_id: UUID,
    values: SaleInput | dict[str, Any],
    actor_id: int,
    update_id: int,
    *,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Sale details must be reviewed and confirmed before saving")
    data = values if isinstance(values, SaleInput) else SaleInput(**values)
    currency = _currency(data.currency)
    opportunity = conn.execute(
        """select o.*,p.currency as property_currency,p.project_name,p.price as asking_price
           from sales_opportunities o left join properties p on p.id=o.primary_property_id
           where o.id=%s for update of o""",
        (opportunity_id,),
    ).fetchone()
    if not opportunity:
        raise ValueError("Opportunity was not found")
    if opportunity["final_outcome"]:
        raise ValueError("Opportunity is already closed")
    if not opportunity["primary_property_id"]:
        raise ValueError("Select a property before recording the sale")
    if opportunity["property_currency"].strip() != currency:
        raise ValueError("Sale currency must match the property's recorded currency")
    if opportunity["asking_price"] is not None and data.recorded_sale_price <= 0:
        raise ValueError("Recorded sale price must be positive")
    row = conn.execute(
        """insert into sale_records
             (opportunity_id,property_id,buyer_contact_id,recorded_sale_price,currency,sale_date,
              responsible_telegram_user_id,payment_plan_summary,supporting_reference,notes,
              verification_status,created_by)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING_VERIFICATION',%s) returning *""",
        (
            opportunity_id,
            opportunity["primary_property_id"],
            opportunity["contact_id"],
            data.recorded_sale_price,
            currency,
            data.sale_date,
            opportunity["assigned_telegram_user_id"] or actor_id,
            data.payment_plan_summary,
            data.supporting_reference,
            data.notes,
            actor_id,
        ),
    ).fetchone()
    rule = _commission_rule(
        conn,
        currency=currency,
        project_name=opportunity["project_name"],
        agent_id=opportunity["assigned_telegram_user_id"] or actor_id,
        sale_date=date.fromisoformat(data.sale_date),
    )
    if rule:
        amount = commission_amount(
            rule["rule_type"], rule["amount_or_rate"], data.recorded_sale_price
        )
        conn.execute(
            """insert into commission_records
                 (sale_record_id,rule_id,calculation_type,rate_or_fixed_amount,calculated_amount,
                  currency,commission_status,updated_by,responsible_telegram_user_id,policy_snapshot)
               values (%s,%s,%s,%s,%s,%s,'ESTIMATED',%s,%s,%s::jsonb)""",
            (
                row["id"],
                rule["id"],
                rule["rule_type"],
                rule["amount_or_rate"],
                amount,
                currency,
                actor_id,
                opportunity["assigned_telegram_user_id"] or actor_id,
                _json(
                    {
                        "rule_id": rule["id"],
                        "rule_name": rule["rule_name"],
                        "rule_type": rule["rule_type"],
                        "amount_or_rate": rule["amount_or_rate"],
                        "currency": rule["currency"],
                        "project_name": rule["project_name"],
                        "agent_telegram_id": rule["agent_telegram_user_id"],
                        "effective_from": rule["effective_from"],
                        "effective_to": rule["effective_to"],
                        "sale_value": data.recorded_sale_price,
                        "sale_date": data.sale_date,
                    }
                ),
            ),
        )
    old_stage = opportunity["current_stage"]
    conn.execute(
        """update sales_opportunities set current_stage='WON',final_outcome='WON',closed_at=now(),
             next_action=null,next_action_due=null,updated_at=now(),last_interaction_at=now()
           where id=%s""",
        (opportunity_id,),
    )
    _opportunity_event(
        conn,
        opportunity_id=opportunity_id,
        event_type="WON",
        actor_id=actor_id,
        update_id=update_id,
        previous_stage=old_stage,
        new_stage="WON",
        details={"sale_id": row["id"], "verification_status": "PENDING_VERIFICATION"},
    )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="SALE_RECORD_CREATED",
        entity_type="SALE",
        entity_id=row["id"],
        contact_id=opportunity["contact_id"],
        new_value={
            "recorded_sale_price": data.recorded_sale_price,
            "currency": currency,
            "sale_date": data.sale_date,
            "verification_status": "PENDING_VERIFICATION",
        },
        request_key=f"{actor_id}:{update_id}" if update_id else None,
    )
    return row


def set_sale_verification(
    conn: Connection,
    sale_id: UUID,
    status: str,
    actor_id: int,
    *,
    confirmed: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Sale verification changes require confirmation")
    if status not in {"VERIFIED", "PENDING_VERIFICATION", "DISPUTED", "CANCELLED"}:
        raise ValueError("Choose a supported verification status")
    old = conn.execute("select * from sale_records where id=%s for update", (sale_id,)).fetchone()
    if not old:
        raise ValueError("Sale record was not found")
    if old["verification_status"] == "CANCELLED":
        raise ValueError("Cancelled sale records cannot be changed")
    if status == "CANCELLED" and old["verification_status"] == "VERIFIED":
        raise ValueError("A verified sale must be disputed and reviewed before correction")
    if status == "PENDING_VERIFICATION" and old["verification_status"] == "VERIFIED":
        raise ValueError("A verified sale must be disputed before its status can change")
    opportunity = conn.execute(
        "select current_stage from sales_opportunities where id=%s for update",
        (old["opportunity_id"],),
    ).fetchone()
    if not opportunity:
        raise ValueError("Sale opportunity was not found")
    row = conn.execute(
        """update sale_records set verification_status=%s,
             verified_by=case when %s='VERIFIED' then %s else verified_by end,
             verified_at=case when %s='VERIFIED' then now() else verified_at end,
             cancelled_at=case when %s='CANCELLED' then now() else cancelled_at end,
             updated_at=now() where id=%s returning *""",
        (status, status, actor_id, status, status, sale_id),
    ).fetchone()
    if status == "CANCELLED":
        conn.execute(
            "update sales_opportunities set current_stage='ON_HOLD',final_outcome=null,closed_at=null,updated_at=now() where id=%s",
            (old["opportunity_id"],),
        )
        _opportunity_event(
            conn,
            opportunity_id=old["opportunity_id"],
            event_type="STAGE_CHANGED",
            actor_id=actor_id,
            previous_stage=opportunity["current_stage"],
            new_stage="ON_HOLD",
            details={"sale_id": sale_id, "verification_status": "CANCELLED"},
        )
        conn.execute(
            "update commission_records set commission_status='CANCELLED',updated_by=%s,updated_at=now() where sale_record_id=%s",
            (actor_id, sale_id),
        )
        other_verified = conn.execute(
            """select 1 from sale_records where buyer_contact_id=%s and id<>%s
                 and verification_status='VERIFIED' limit 1""",
            (old["buyer_contact_id"], sale_id),
        ).fetchone()
        if not other_verified:
            conn.execute(
                """update contacts set conversion_state='NOT_CONVERTED',
                     lead_status=case when lead_status='CONVERTED' then 'INTERESTED' else lead_status end,
                     updated_at=now() where id=%s""",
                (old["buyer_contact_id"],),
            )
    elif status == "VERIFIED":
        conn.execute(
            """update contacts set conversion_state='CONVERTED',
                 lead_status=case when lead_status='DO_NOT_CONTACT' then lead_status else 'CONVERTED' end,
                 updated_at=now() where id=%s""",
            (old["buyer_contact_id"],),
        )
        conn.execute(
            """update commission_records set commission_status=case
                 when commission_status='ESTIMATED' then 'CALCULATED' else commission_status end,
                 updated_by=%s,updated_at=now() where sale_record_id=%s""",
            (actor_id, sale_id),
        )
    elif status == "DISPUTED":
        conn.execute(
            "update commission_records set commission_status='DISPUTED',updated_by=%s,updated_at=now() where sale_record_id=%s",
            (actor_id, sale_id),
        )
        if old["verification_status"] == "VERIFIED":
            other_verified = conn.execute(
                """select 1 from sale_records where buyer_contact_id=%s and id<>%s
                     and verification_status='VERIFIED' limit 1""",
                (old["buyer_contact_id"], sale_id),
            ).fetchone()
            if not other_verified:
                conn.execute(
                    """update contacts set conversion_state='NOT_CONVERTED',
                         lead_status=case when lead_status='CONVERTED' then 'INTERESTED' else lead_status end,
                         updated_at=now() where id=%s""",
                    (old["buyer_contact_id"],),
                )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="SALE_VERIFICATION_CHANGED",
        entity_type="SALE",
        entity_id=sale_id,
        contact_id=old["buyer_contact_id"],
        old_value={"verification_status": old["verification_status"]},
        new_value={"verification_status": status},
        reason=reason,
    )
    return row


def add_commission_rule(
    conn: Connection,
    *,
    rule_name: str,
    rule_type: str,
    amount_or_rate: Decimal,
    currency: str,
    actor_id: int,
    project_name: str | None = None,
    agent_id: int | None = None,
    effective_from: date | None = None,
    effective_to: date | None = None,
) -> dict[str, Any]:
    currency = _currency(currency)
    if rule_type not in {"FIXED", "PERCENTAGE"}:
        raise ValueError("Commission type must be FIXED or PERCENTAGE")
    amount = Decimal(str(amount_or_rate))
    if amount < 0 or (rule_type == "PERCENTAGE" and amount > 100):
        raise ValueError("Commission rate must be between 0 and 100")
    if amount.as_tuple().exponent < -4 or amount > Decimal("99999999.9999"):
        raise ValueError("Commission rule amount must fit up to four decimal places")
    if not rule_name.strip() or len(rule_name) > 160:
        raise ValueError("Commission rule name must contain 1 to 160 characters")
    if effective_from and effective_to and effective_to < effective_from:
        raise ValueError("Commission rule end date cannot be before its start date")
    row = conn.execute(
        """insert into commission_rules
             (rule_name,rule_type,amount_or_rate,currency,project_name,agent_telegram_user_id,
              effective_from,effective_to,created_by)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
        (
            rule_name.strip(),
            rule_type,
            amount,
            currency,
            project_name,
            agent_id,
            effective_from,
            effective_to,
            actor_id,
        ),
    ).fetchone()
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="COMMISSION_RULE_CREATED",
        entity_type="COMMISSION_RULE",
        entity_id=row["id"],
        new_value={
            "name": row["rule_name"],
            "type": row["rule_type"],
            "amount_or_rate": row["amount_or_rate"],
            "currency": row["currency"],
            "project_name": row["project_name"],
            "agent_telegram_user_id": row["agent_telegram_user_id"],
        },
    )
    return row


def update_commission(
    conn: Connection,
    commission_id: UUID,
    *,
    status: str | None,
    amount: Decimal | None,
    actor_id: int,
    confirmed: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Commission changes require confirmation")
    if status is not None and status not in COMMISSION_STATUSES:
        raise ValueError("Choose a supported commission status")
    if amount is not None and Decimal(str(amount)) < 0:
        raise ValueError("Commission amount cannot be negative")
    if amount is not None and (
        Decimal(str(amount)).as_tuple().exponent < -2
        or Decimal(str(amount)) > Decimal("999999999999.99")
    ):
        raise ValueError("Commission amount must fit a 2-decimal financial amount")
    if amount is not None and (not reason or not reason.strip() or len(reason.strip()) > 500):
        raise ValueError("Record a reason of up to 500 characters for a commission adjustment")
    old = conn.execute(
        "select * from commission_records where id=%s for update", (commission_id,)
    ).fetchone()
    if not old:
        raise ValueError("Commission record was not found")
    new_status = status or old["commission_status"]
    if old["commission_status"] == "CANCELLED":
        raise ValueError("Cancelled commission records cannot be changed")
    if old["commission_status"] == "PAID" and (
        amount is not None or new_status not in {"PAID", "DISPUTED"}
    ):
        raise ValueError("A paid commission cannot be changed except to record a dispute")
    if amount is not None and Decimal(str(old.get("paid_amount") or 0)) > 0:
        raise ValueError("A commission with recorded payments cannot be adjusted; record a dispute")
    if new_status == "PAID" and status != "PAID":
        raise ValueError("Marking commission paid requires an explicit Paid action")
    if new_status == "PAID" and old["commission_status"] != "CONFIRMED":
        raise ValueError("Confirm the commission amount before recording payment")
    if new_status == "PAID":
        raise ValueError("Use the payment record flow with an amount and payment reference")
    if amount is not None and old["commission_status"] == "PAID":
        raise ValueError("A paid commission cannot be edited; record a dispute for correction")
    row = conn.execute(
        """update commission_records set commission_status=%s,
             calculation_type=case when %s is not null then 'MANUAL' else calculation_type end,
             rate_or_fixed_amount=coalesce(%s,rate_or_fixed_amount),
             calculated_amount=coalesce(%s,calculated_amount),
             approved_amount=case when %s in ('CONFIRMED','APPROVED') then coalesce(%s,calculated_amount) else approved_amount end,
             updated_by=%s,updated_at=now() where id=%s returning *""",
        (new_status, amount, amount, amount, new_status, amount, actor_id, commission_id),
    ).fetchone()
    if amount is not None and old["commission_status"] in {"CONFIRMED", "APPROVED"}:
        row = conn.execute(
            "update commission_records set approved_amount=calculated_amount where id=%s returning *",
            (commission_id,),
        ).fetchone()
    conn.execute(
        """insert into commission_approval_history(commission_record_id,actor_telegram_user_id,
             action,previous_value,new_value,reason)
           values (%s,%s,%s,%s::jsonb,%s::jsonb,%s)""",
        (
            commission_id,
            actor_id,
            "ADJUSTED"
            if amount is not None
            else "APPROVED"
            if new_status in {"CONFIRMED", "APPROVED"}
            else "DISPUTED"
            if new_status == "DISPUTED"
            else "REJECTED",
            _json({"amount": old["calculated_amount"], "status": old["commission_status"]}),
            _json({"amount": row["calculated_amount"], "status": row["commission_status"]}),
            reason.strip()
            if reason and reason.strip()
            else f"Commission status changed to {new_status}",
        ),
    )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="COMMISSION_CHANGED",
        entity_type="COMMISSION",
        entity_id=commission_id,
        old_value={
            "amount": old["calculated_amount"],
            "status": old["commission_status"],
            "calculation_type": old["calculation_type"],
        },
        new_value={
            "amount": row["calculated_amount"],
            "status": row["commission_status"],
            "calculation_type": row["calculation_type"],
        },
        reason=reason.strip() if reason and reason.strip() else None,
    )
    return row


def record_commission_payment(
    conn: Connection,
    commission_id: UUID,
    *,
    amount: Decimal,
    reference: str,
    actor_id: int,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Commission payment requires confirmation")
    from realestate_crm.services.team import require_permission

    require_permission(conn, actor_id, "commissions.manage")
    payment = Decimal(str(amount))
    reference = reference.strip()
    if not payment.is_finite() or payment <= 0 or payment.as_tuple().exponent < -2:
        raise ValueError("Payment amount must be positive and fit two decimal places")
    if len(reference) < 2 or len(reference) > 120:
        raise ValueError("Enter a payment reference from 2 to 120 characters")
    row = conn.execute(
        "select * from commission_records where id=%s for update", (commission_id,)
    ).fetchone()
    if not row:
        raise ValueError("Commission record was not found")
    if row["commission_status"] not in {"CONFIRMED", "APPROVED", "PARTIALLY_PAID"}:
        raise ValueError("Approve the commission before recording a payment")
    approved = Decimal(
        str(
            row["approved_amount"]
            if row["approved_amount"] is not None
            else row["calculated_amount"]
        )
    )
    paid = Decimal(str(row["paid_amount"] or 0))
    if payment > approved - paid:
        raise ValueError("Payment exceeds the approved outstanding amount")
    new_paid = paid + payment
    new_status = "PAID" if new_paid == approved else "PARTIALLY_PAID"
    updated = conn.execute(
        """update commission_records set commission_status=%s,approved_amount=%s,paid_amount=%s,
             paid_at=case when %s='PAID' then now() else paid_at end,payment_reference=%s,
             updated_by=%s,updated_at=now() where id=%s returning *""",
        (new_status, approved, new_paid, new_status, reference, actor_id, commission_id),
    ).fetchone()
    conn.execute(
        """insert into commission_approval_history(commission_record_id,actor_telegram_user_id,
             action,previous_value,new_value,reason)
           values (%s,%s,'PAYMENT_RECORDED',%s::jsonb,%s::jsonb,%s)""",
        (
            commission_id,
            actor_id,
            _json({"paid_amount": paid, "status": row["commission_status"]}),
            _json(
                {
                    "paid_amount": new_paid,
                    "status": new_status,
                    "payment_reference": reference,
                    "amount": payment,
                }
            ),
            f"Payment recorded with reference {reference}",
        ),
    )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="COMMISSION_PAYMENT_RECORDED",
        entity_type="COMMISSION",
        entity_id=commission_id,
        old_value={"paid_amount": paid, "status": row["commission_status"]},
        new_value={"paid_amount": new_paid, "status": new_status, "payment_reference": reference},
        reason=f"Payment reference {reference}",
    )
    return updated


def update_commission_rule(
    conn: Connection,
    rule_id: UUID,
    *,
    active: bool,
    actor_id: int,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Commission rule changes require confirmation")
    old = conn.execute(
        "select * from commission_rules where id=%s for update", (rule_id,)
    ).fetchone()
    if not old:
        raise ValueError("Commission rule was not found")
    row = conn.execute(
        "update commission_rules set active=%s where id=%s returning *",
        (active, rule_id),
    ).fetchone()
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="COMMISSION_RULE_CHANGED",
        entity_type="COMMISSION_RULE",
        entity_id=rule_id,
        old_value={"active": old["active"]},
        new_value={"active": row["active"]},
    )
    return row


def record_manual_commission(
    conn: Connection,
    sale_id: UUID,
    amount: Decimal,
    actor_id: int,
    *,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Review and confirm the manual commission amount")
    amount = Decimal(str(amount))
    if amount < 0:
        raise ValueError("Commission cannot be negative")
    if amount.as_tuple().exponent < -2 or amount > Decimal("999999999999.99"):
        raise ValueError("Commission amount must fit a 2-decimal financial amount")
    sale = conn.execute(
        "select verification_status,currency,buyer_contact_id,responsible_telegram_user_id,recorded_sale_price,sale_date from sale_records where id=%s for update",
        (sale_id,),
    ).fetchone()
    if not sale or sale["verification_status"] == "CANCELLED":
        raise ValueError("An active sale record is required")
    row = conn.execute(
        """insert into commission_records
             (sale_record_id,calculation_type,rate_or_fixed_amount,calculated_amount,currency,
              commission_status,updated_by,responsible_telegram_user_id,policy_snapshot)
           values (%s,'MANUAL',%s,%s,%s,%s,%s,%s,%s::jsonb)
           on conflict (sale_record_id) do nothing returning *""",
        (
            sale_id,
            amount,
            amount,
            sale["currency"],
            "CALCULATED"
            if sale["verification_status"] == "VERIFIED"
            else "DISPUTED"
            if sale["verification_status"] == "DISPUTED"
            else "ESTIMATED",
            actor_id,
            sale["responsible_telegram_user_id"],
            _json(
                {
                    "method": "MANUAL",
                    "basis": "administrator-entered",
                    "sale_value": sale["recorded_sale_price"],
                    "currency": sale["currency"],
                    "sale_date": sale["sale_date"],
                }
            ),
        ),
    ).fetchone()
    if not row:
        raise ValueError("A commission record already exists for this sale")
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="COMMISSION_MANUAL_ENTRY",
        entity_type="COMMISSION",
        entity_id=row["id"],
        contact_id=sale["buyer_contact_id"],
        new_value={
            "amount": amount,
            "currency": sale["currency"],
            "status": row["commission_status"],
        },
    )
    return row


def ensure_priority_weights(conn: Connection, telegram_user_id: int) -> dict[str, int]:
    for code, weight in DEFAULT_PRIORITY_WEIGHTS.items():
        conn.execute(
            """insert into sales_priority_weights (telegram_user_id,priority_code,weight)
               values (%s,%s,%s) on conflict (telegram_user_id,priority_code) do nothing""",
            (telegram_user_id, code, weight),
        )
    rows = conn.execute(
        "select priority_code,weight from sales_priority_weights where telegram_user_id=%s",
        (telegram_user_id,),
    ).fetchall()
    return {row["priority_code"]: row["weight"] for row in rows}


def set_priority_weight(conn: Connection, telegram_user_id: int, code: str, weight: int) -> None:
    if code not in DEFAULT_PRIORITY_WEIGHTS or not 0 <= weight <= 1000:
        raise ValueError("Choose a priority rule and a weight from 0 to 1,000")
    old = conn.execute(
        "select weight from sales_priority_weights where telegram_user_id=%s and priority_code=%s",
        (telegram_user_id, code),
    ).fetchone()
    conn.execute(
        """insert into sales_priority_weights (telegram_user_id,priority_code,weight)
           values (%s,%s,%s) on conflict (telegram_user_id,priority_code) do update
           set weight=excluded.weight,updated_at=now()""",
        (telegram_user_id, code, weight),
    )
    _record_audit(
        conn,
        actor_id=telegram_user_id,
        event_type="SALES_PRIORITY_WEIGHT_CHANGED",
        entity_type="SALES_PRIORITY_RULE",
        old_value={"priority_code": code, "weight": old["weight"]} if old else None,
        new_value={"priority_code": code, "weight": weight},
    )


def command_center_metrics(conn: Connection, now: datetime, timezone: str) -> dict[str, Any]:
    start, end = local_day_bounds(now, timezone)
    counts = conn.execute(
        """select
             (select count(*) from call_logs where created_at >= %s and created_at < %s) calls_logged,
             (select count(*) from call_logs where created_at >= %s and created_at < %s and connection_result='ANSWERED') answered,
             (select count(*) from lead_qualifications where qualification_status in ('QUALIFIED','HIGH_PRIORITY_QUALIFIED') and updated_at >= %s and updated_at < %s) qualified,
             (select count(*) from follow_ups where status='COMPLETED' and completed_at >= %s and completed_at < %s) followups_completed,
             (select count(*) from follow_ups where status='PENDING' and scheduled_at < %s) followups_overdue,
             (select count(*) from property_visits where scheduled_at >= %s and scheduled_at < %s and visit_status in ('SCHEDULED','BUYER_ARRIVED')) visits_scheduled,
             (select count(*) from property_visits where completed_at >= %s and completed_at < %s and visit_status='COMPLETED') visits_completed,
             (select count(*) from negotiation_records where negotiation_status not in ('REJECTED','EXPIRED','CANCELLED')) active_negotiations,
             (select count(*) from reservations where created_at >= %s and created_at < %s and reservation_status not in ('CANCELLED','EXPIRED')) reservations_recorded,
             (select count(*) from sale_records where created_at >= %s and created_at < %s and verification_status <> 'CANCELLED') sales_recorded,
             (select count(distinct s.opportunity_id) from sale_records s
                where s.verification_status='VERIFIED' and s.verified_at >= %s and s.verified_at < %s) opportunities_won,
             (select count(*) from sales_opportunities where final_outcome='LOST' and closed_at >= %s and closed_at < %s) opportunities_lost,
             (select coalesce(jsonb_object_agg(currency,amount), '{}'::jsonb) from (
                select currency,sum(estimated_sale_value) amount from sales_opportunities
                where final_outcome is null and estimated_sale_value is not null group by currency
              ) values_by_currency) estimated_pipeline_by_currency,
             (select count(*) from sales_opportunities where final_outcome is null and next_action is null) without_next_action""",
        (
            start,
            end,
            start,
            end,
            start,
            end,
            start,
            end,
            now,
            start,
            end,
            start,
            end,
            start,
            end,
            start,
            end,
            start,
            end,
            start,
            end,
        ),
    ).fetchone()
    previous_start, previous_end = local_day_bounds(now - timedelta(days=1), timezone)
    previous = conn.execute(
        """select
             (select count(*) from call_logs where created_at>=%s and created_at<%s) calls_logged,
             (select count(*) from call_logs where created_at>=%s and created_at<%s
                and (connection_result='ANSWERED' or (outcome_category='CONNECTION' and outcome_code='ANSWERED'))) answered,
             (select count(*) from follow_ups where status='COMPLETED' and completed_at>=%s and completed_at<%s) followups_completed,
             (select count(*) from property_visits where visit_status='COMPLETED' and completed_at>=%s and completed_at<%s) visits_completed,
             (select count(*) from sale_records where verification_status<>'CANCELLED' and created_at>=%s and created_at<%s) sales_recorded""",
        (
            previous_start,
            previous_end,
            previous_start,
            previous_end,
            previous_start,
            previous_end,
            previous_start,
            previous_end,
            previous_start,
            previous_end,
        ),
    ).fetchone()
    return {**counts, "previous_day": previous, "timezone": timezone}


def priority_queue(conn: Connection, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    ensure_priority_weights(conn, user_id)
    limit = max(1, min(limit, 50))
    return conn.execute(
        """with items as (
             (select 'BOOKING_DUE'::text priority_code,o.id opportunity_id,o.contact_id,
                     coalesce(r.expires_at,o.next_action_due) due_at,
                     'Review booking or closing task'::text action,o.current_stage stage
                from sales_opportunities o left join reservations r on r.opportunity_id=o.id
                  and r.reservation_status in ('REQUESTED','AWAITING_CONFIRMATION','CONFIRMED')
               where o.final_outcome is null and o.current_stage in ('BOOKED','RESERVATION_IN_PROGRESS')
                 and coalesce(r.expires_at,o.next_action_due) <= now()+interval '3 days'
               order by due_at nulls last,o.id limit 50)
             union all
             (select 'VISIT_CONFIRMATION',v.opportunity_id,v.contact_id,v.scheduled_at,
                     'Confirm upcoming property visit',o.current_stage
                from property_visits v join sales_opportunities o on o.id=v.opportunity_id
               where o.final_outcome is null and v.visit_status='SCHEDULED'
                 and v.confirmation_status<>'CONFIRMED'
                 and v.scheduled_at between now() and now()+interval '24 hours'
               order by v.scheduled_at,v.id limit 50)
             union all
             (select case when q.qualification_status='HIGH_PRIORITY_QUALIFIED' then 'OVERDUE_QUALIFIED' else 'OTHER_DUE' end,
                     f.opportunity_id,f.contact_id,f.scheduled_at,coalesce(f.reason,f.follow_up_type),o.current_stage
                from follow_ups f join contacts c on c.id=f.contact_id
                  left join sales_opportunities o on o.id=f.opportunity_id
                  left join lead_qualifications q on q.contact_id=c.id
               where f.status='PENDING' and f.scheduled_at<=now()
                 and c.contact_eligibility<>'DO_NOT_CONTACT' and c.lead_status<>'DO_NOT_CONTACT'
               order by f.scheduled_at,f.priority desc,f.id limit 50)
             union all
             (select 'NEGOTIATION_DUE',o.id,o.contact_id,n.next_action_due,
                     coalesce(n.next_action,'Review negotiation'),o.current_stage
                from negotiation_records n join sales_opportunities o on o.id=n.opportunity_id
                join contacts c on c.id=o.contact_id
               where o.final_outcome is null and n.negotiation_status not in ('REJECTED','EXPIRED','CANCELLED')
                 and n.next_action_due<=now()+interval '1 day'
                 and c.contact_eligibility<>'DO_NOT_CONTACT' and c.lead_status<>'DO_NOT_CONTACT'
               order by n.next_action_due,n.id limit 50)
             union all
             (select 'HIGH_QUALIFIED',o.id,o.contact_id,o.next_action_due,
                     coalesce(o.next_action,'Review high-priority buyer'),o.current_stage
                from sales_opportunities o join lead_qualifications q on q.contact_id=o.contact_id
                join contacts c on c.id=o.contact_id
               where o.final_outcome is null and q.qualification_status='HIGH_PRIORITY_QUALIFIED'
                 and o.next_action_due between now() and now()+interval '1 day'
                 and c.contact_eligibility='ELIGIBLE' and c.lead_status<>'DO_NOT_CONTACT'
               order by o.next_action_due,o.id limit 50)
             union all
             (select 'NEW_PROSPECT',o.id,o.contact_id,o.next_action_due,
                     coalesce(o.next_action,'Contact new opportunity'),o.current_stage
                from sales_opportunities o join contacts c on c.id=o.contact_id
               where o.final_outcome is null and o.current_stage='NEW_OPPORTUNITY'
                 and c.contact_eligibility='ELIGIBLE' and c.lead_status<>'DO_NOT_CONTACT'
               order by o.created_at,o.id limit 50)
           )
           select i.*,c.name,c.phone_number,c.contact_eligibility,w.weight
             from items i join contacts c on c.id=i.contact_id
             join sales_priority_weights w on w.telegram_user_id=%s and w.priority_code=i.priority_code
            order by w.weight desc,i.due_at nulls last,i.opportunity_id limit %s""",
        (user_id, limit),
    ).fetchall()


def command_center_sections(
    conn: Connection, now: datetime, limit: int = 10, timezone: str = "Africa/Addis_Ababa"
) -> dict[str, list[dict[str, Any]]]:
    limit = max(1, min(limit, 25))
    day_start, day_end = local_day_bounds(now, timezone)
    return {
        "followups": conn.execute(
            """select f.id,f.contact_id,f.opportunity_id,f.follow_up_type,f.reason,f.scheduled_at,f.status,
                      c.name,c.phone_number,o.current_stage from follow_ups f join contacts c on c.id=f.contact_id
               left join sales_opportunities o on o.id=f.opportunity_id
               where f.status='PENDING' and f.scheduled_at<=%s order by f.scheduled_at,f.priority desc limit %s""",
            (now, limit),
        ).fetchall(),
        "visits": conn.execute(
            """select v.id,v.opportunity_id,v.scheduled_at,v.confirmation_status,v.visit_status,
                      c.name,p.title property_title from property_visits v join contacts c on c.id=v.contact_id
               join properties p on p.id=v.property_id where v.visit_status in ('SCHEDULED','BUYER_ARRIVED')
                 and v.scheduled_at>=%s and v.scheduled_at<%s
               order by v.scheduled_at limit %s""",
            (day_start, day_end, limit),
        ).fetchall(),
        "negotiations": conn.execute(
            """select n.id,n.opportunity_id,n.negotiation_status,n.next_action,n.next_action_due,
                      c.name,o.currency,n.current_discussed_price from negotiation_records n
               join sales_opportunities o on o.id=n.opportunity_id join contacts c on c.id=o.contact_id
               where n.negotiation_status not in ('REJECTED','EXPIRED','CANCELLED')
               order by n.next_action_due nulls last,n.updated_at desc limit %s""",
            (limit,),
        ).fetchall(),
        "reservations": conn.execute(
            """select r.id,r.opportunity_id,r.reservation_status,r.payment_status,r.expires_at,
                      r.reservation_amount,r.currency,c.name,p.title property_title
               from reservations r join sales_opportunities o on o.id=r.opportunity_id
               join contacts c on c.id=o.contact_id join properties p on p.id=r.property_id
               where r.reservation_status in ('REQUESTED','AWAITING_CONFIRMATION','CONFIRMED')
               order by r.expires_at nulls last,r.created_at desc limit %s""",
            (limit,),
        ).fetchall(),
        "closing": conn.execute(
            """select o.id,o.expected_closing_date,o.estimated_sale_value,o.currency,c.name,o.current_stage
               from sales_opportunities o join contacts c on c.id=o.contact_id
               where o.final_outcome is null and o.expected_closing_date between current_date and current_date+14
               order by o.expected_closing_date,o.id limit %s""",
            (limit,),
        ).fetchall(),
        "recent_sales": conn.execute(
            """select s.id,s.recorded_sale_price,s.currency,s.sale_date,s.verification_status,c.name,p.title property_title
               from sale_records s join contacts c on c.id=s.buyer_contact_id join properties p on p.id=s.property_id
               where s.verification_status<>'CANCELLED' order by s.created_at desc limit %s""",
            (limit,),
        ).fetchall(),
        "lost": conn.execute(
            """select id,contact_id,lost_reason,lost_explanation,closed_at from sales_opportunities
               where final_outcome='LOST' order by closed_at desc limit %s""",
            (limit,),
        ).fetchall(),
    }


def refresh_reactivation_queue(conn: Connection, limit: int = 100) -> int:
    limit = max(1, min(limit, 500))
    inserted = conn.execute(
        """with candidates as (
             select c.id contact_id,null::uuid opportunity_id,'QUALIFIED_INACTIVE'::text rule_code,
                    'Qualified buyer has no recent recorded call'::text rationale,
                    jsonb_build_object('last_called_at',c.last_called_at,'qualification',q.qualification_status) relevant_facts,
                    'Review buyer requirements and choose a next action'::text suggested_action,
                    null::timestamptz recommended_followup_at
               from contacts c join lead_qualifications q on q.contact_id=c.id
              where q.qualification_status in ('QUALIFIED','HIGH_PRIORITY_QUALIFIED')
                and (c.last_called_at is null or c.last_called_at<now()-interval '14 days')
             union all
             select c.id,o.id,'CALLBACK_DUE','A completed callback date has passed',
                    jsonb_build_object('scheduled_at',f.scheduled_at,'last_called_at',c.last_called_at),
                    'Review the callback and schedule or complete the next action',null::timestamptz
               from follow_ups f join contacts c on c.id=f.contact_id
                 left join sales_opportunities o on o.id=f.opportunity_id
              where f.status in ('COMPLETED','MISSED') and f.follow_up_type='CALL_BACK'
                and f.scheduled_at<now()-interval '30 days' and c.last_called_at<f.scheduled_at
             union all
             select r.contact_id,o.id,'PROPERTY_AVAILABLE','A previously unavailable recommended property is now recorded available',
                    jsonb_build_object('property_id',p.id,'property_title',p.title,'availability',p.availability_status),
                    'Review the property record before contacting the buyer',null::timestamptz
               from property_recommendations r join properties p on p.id=r.property_id
                 left join sales_opportunities o on o.contact_id=r.contact_id and o.final_outcome is null
              where r.recommendation_status='NO_LONGER_AVAILABLE' and p.availability_status='AVAILABLE'
             union all
             select r.contact_id,o.id,'LOWER_PRICE_ALTERNATIVE','The buyer previously recorded a price concern',
                    jsonb_build_object('reason',r.rejection_reason,'property_id',p.id),
                    'Find a lower-priced available alternative',null::timestamptz
               from property_recommendations r join properties p on p.id=r.property_id
                 left join sales_opportunities o on o.contact_id=r.contact_id and o.final_outcome is null
              where r.recommendation_status='REJECTED' and r.rejection_reason='PRICE_TOO_HIGH'
             union all
             select c.id,o.id,'TIMELINE_APPROACHING','Buyer purchase timeline is approaching',
                    jsonb_build_object('expected_timeline',i.expected_timeline),
                    'Review requirements and propose suitable available properties',null::timestamptz
               from contact_property_interests i join contacts c on c.id=i.contact_id
                 left join sales_opportunities o on o.contact_id=c.id and o.final_outcome is null
              where i.expected_timeline in ('IMMEDIATELY','WITHIN_1_MONTH')
                and (c.last_called_at is null or c.last_called_at<now()-interval '7 days')
           ), eligible as (
             select x.* from candidates x join contacts c on c.id=x.contact_id
              where c.contact_eligibility='ELIGIBLE'
                and c.lead_status not in ('DO_NOT_CONTACT','NOT_INTERESTED','DISQUALIFIED','CONVERTED')
                and c.conversion_state<>'CONVERTED'
                and not exists (select 1 from follow_ups f where f.contact_id=c.id and f.status='PENDING'
                                  and f.scheduled_at>now())
                and (c.conversion_state<>'CONVERTED' or x.opportunity_id is not null)
                and (
                  exists (select 1 from sales_opportunities active where active.contact_id=c.id and active.final_outcome is null)
                  or not exists (select 1 from sales_opportunities closed where closed.contact_id=c.id
                                  and closed.final_outcome='LOST' and not closed.future_followup_allowed)
                )
                and not exists (select 1 from reactivation_recommendations r
                                  where r.contact_id=x.contact_id and r.rule_code=x.rule_code and r.status='OPEN')
                and not exists (select 1 from reactivation_recommendations previous
                                  where previous.contact_id=x.contact_id and previous.rule_code=x.rule_code
                                    and previous.status in ('ACTIONED','SCHEDULED','DISMISSED')
                                    and previous.generated_at>=now()-interval '90 days')
             order by x.rationale,x.contact_id limit %s
           )
           insert into reactivation_recommendations
             (contact_id,opportunity_id,rule_code,rationale,relevant_facts,suggested_action,recommended_followup_at)
           select contact_id,opportunity_id,rule_code,rationale,relevant_facts,suggested_action,recommended_followup_at
           from eligible on conflict do nothing returning id""",
        (limit,),
    ).fetchall()
    return len(inserted)


def list_reactivation_candidates(
    conn: Connection, page: int = 0, page_size: int = 8
) -> list[dict[str, Any]]:
    page = max(0, min(int(page), 1000))
    page_size = max(1, min(int(page_size), 20))
    refresh_reactivation_queue(conn, limit=100)
    return conn.execute(
        """select r.*,c.name,c.phone_number,c.normalized_phone,c.contact_eligibility,
                  c.lead_status,c.last_called_at,o.current_stage
           from reactivation_recommendations r join contacts c on c.id=r.contact_id
           left join sales_opportunities o on o.id=r.opportunity_id
           where r.status='OPEN' and c.contact_eligibility='ELIGIBLE'
             and c.lead_status not in ('DO_NOT_CONTACT','NOT_INTERESTED','DISQUALIFIED','CONVERTED')
             and c.conversion_state<>'CONVERTED'
           order by r.recommended_followup_at nulls first,r.generated_at,r.id
           limit %s offset %s""",
        (page_size, page * page_size),
    ).fetchall()


def act_on_reactivation(
    conn: Connection,
    recommendation_id: UUID,
    action: str,
    actor_id: int,
    *,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Confirm the selected reactivation action")
    if action not in {"SCHEDULE_FOLLOWUP", "FIND_ALTERNATIVES", "DISMISS", "CALL_NOW"}:
        raise ValueError("Choose a supported reactivation action")
    row = conn.execute(
        """select r.*,c.contact_eligibility,c.lead_status,c.conversion_state,
                  c.phone_number,c.normalized_phone
           from reactivation_recommendations r join contacts c on c.id=r.contact_id
           where r.id=%s for update of r,c""",
        (recommendation_id,),
    ).fetchone()
    if not row or row["status"] != "OPEN":
        raise ValueError("This reactivation suggestion is no longer open")
    if action == "CALL_NOW":
        if not contact_is_callable(row):
            raise ValueError("This contact is not eligible for calling")
        if conn.execute(
            "select id from follow_ups where contact_id=%s and status='PENDING' and scheduled_at>now() limit 1",
            (row["contact_id"],),
        ).fetchone():
            raise ValueError("This contact already has a future callback")
        # This only returns the contact to the agent. It never places a call.
        conn.execute(
            "update reactivation_recommendations set status='ACTIONED',acted_at=now() where id=%s",
            (recommendation_id,),
        )
    elif action == "DISMISS":
        conn.execute(
            "update reactivation_recommendations set status='DISMISSED',acted_at=now(),dismissed_reason='Agent dismissed' where id=%s",
            (recommendation_id,),
        )
    elif action == "SCHEDULE_FOLLOWUP":
        conn.execute(
            "update reactivation_recommendations set status='SCHEDULED',acted_at=now() where id=%s",
            (recommendation_id,),
        )
    else:
        conn.execute(
            "update reactivation_recommendations set status='ACTIONED',acted_at=now() where id=%s",
            (recommendation_id,),
        )
    _record_audit(
        conn,
        actor_id=actor_id,
        event_type="REACTIVATION_ACTION_SELECTED",
        entity_type="REACTIVATION",
        entity_id=recommendation_id,
        contact_id=row["contact_id"],
        new_value={
            "action": action,
            "status": "DISMISSED"
            if action == "DISMISS"
            else "SCHEDULED"
            if action == "SCHEDULE_FOLLOWUP"
            else "ACTIONED",
        },
    )
    return {**row, "action": action}


def report_pipeline(
    conn: Connection,
    start: datetime,
    end: datetime,
    timezone: str = "Africa/Addis_Ababa",
) -> dict[str, Any]:
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("Report range must use timezone-aware, increasing dates")
    from zoneinfo import ZoneInfo

    start_day = start.astimezone(ZoneInfo(timezone)).date()
    end_day = end.astimezone(ZoneInfo(timezone)).date()
    summary = conn.execute(
        """select count(*) filter (where final_outcome is null) active,
                  count(*) filter (where final_outcome='WON' and exists (
                    select 1 from sale_records s where s.opportunity_id=sales_opportunities.id
                      and s.verification_status='VERIFIED' and s.verified_at>=%s and s.verified_at<%s)) won,
                  count(*) filter (where final_outcome='LOST' and closed_at>=%s and closed_at<%s) lost,
                  count(*) filter (where final_outcome is null and next_action is null) without_next_action,
                  count(*) filter (where final_outcome is null and next_action_due is not null and next_action_due<now()) overdue_actions
           from sales_opportunities""",
        (start.astimezone(UTC), end.astimezone(UTC), start.astimezone(UTC), end.astimezone(UTC)),
    ).fetchone()
    stages = conn.execute(
        """select s.stage_code,s.display_name,count(o.id) count from sales_pipeline_stages s
           left join sales_opportunities o on o.current_stage=s.stage_code
           where s.enabled group by s.stage_code,s.display_name,s.sort_order order by s.sort_order"""
    ).fetchall()
    losses = conn.execute(
        """select lost_reason,count(*) count from sales_opportunities
           where final_outcome='LOST' and closed_at>=%s and closed_at<%s
           group by lost_reason order by count(*) desc,lost_reason""",
        (start.astimezone(UTC), end.astimezone(UTC)),
    ).fetchall()
    pipeline_values = conn.execute(
        """select currency,coalesce(sum(estimated_sale_value),0) amount from sales_opportunities
           where final_outcome is null and estimated_sale_value is not null group by currency order by currency"""
    ).fetchall()
    sales_values = conn.execute(
        """select currency,sum(recorded_sale_price) amount from sale_records
           where sale_date>=%s and sale_date<%s and verification_status='VERIFIED'
           group by currency order by currency""",
        (start_day, end_day),
    ).fetchall()
    commission_values = conn.execute(
        """select cr.currency,cr.commission_status,sum(cr.calculated_amount) amount
           from commission_records cr join sale_records s on s.id=cr.sale_record_id
           where s.sale_date>=%s and s.sale_date<%s and s.verification_status<>'CANCELLED'
             and cr.commission_status<>'CANCELLED'
           group by cr.currency,cr.commission_status order by cr.currency,cr.commission_status""",
        (start_day, end_day),
    ).fetchall()
    return {
        **summary,
        "pipeline_values": pipeline_values,
        "sales_values": sales_values,
        "commission_values": commission_values,
        "stages": stages,
        "lost_reasons": losses,
    }


def workflow_report(
    conn: Connection, start: datetime, end: datetime, now: datetime
) -> dict[str, Any]:
    if start.tzinfo is None or end.tzinfo is None or now.tzinfo is None or start >= end:
        raise ValueError("Report range must use timezone-aware, increasing dates")
    row = conn.execute(
        """with due_followups as (
             select id,status from follow_ups where scheduled_at>=%s and scheduled_at<%s
               and scheduled_at<=%s and status not in ('CANCELLED','RESCHEDULED')
           ), visits as (
             select id,attendance_outcome from property_visits where scheduled_at>=%s and scheduled_at<%s
               and attendance_outcome is not null and visit_status not in ('CANCELLED','RESCHEDULED')
           )
           select
             (select count(*) from due_followups) followups_due,
             (select count(*) from due_followups where status='COMPLETED') followups_completed,
             (select count(*) from property_visits where scheduled_at>=%s and scheduled_at<%s
                and visit_status not in ('CANCELLED','RESCHEDULED')) visits_scheduled,
             (select count(*) from visits where attendance_outcome='ATTENDED') visits_attended,
             (select count(*) from visits) visits_resolved,
             (select count(*) from negotiation_records where created_at>=%s and created_at<%s) negotiations_started,
             (select count(*) from sales_opportunities where created_at>=%s and created_at<%s) opportunities_created,
             (select count(distinct opportunity_id) from property_visits where scheduled_at>=%s and scheduled_at<%s
                and visit_status not in ('CANCELLED','RESCHEDULED')) opportunities_with_visit,
             (select count(distinct opportunity_id) from negotiation_records where created_at>=%s and created_at<%s) opportunities_negotiating,
             (select count(*) from reservations where created_at>=%s and created_at<%s
                and reservation_status<>'CANCELLED') reservations_recorded,
             (select count(*) from sale_records where created_at>=%s and created_at<%s
                and verification_status<>'CANCELLED') sales_recorded""",
        (
            start.astimezone(UTC),
            end.astimezone(UTC),
            now.astimezone(UTC),
            start.astimezone(UTC),
            end.astimezone(UTC),
            start.astimezone(UTC),
            end.astimezone(UTC),
            start.astimezone(UTC),
            end.astimezone(UTC),
            start.astimezone(UTC),
            end.astimezone(UTC),
            start.astimezone(UTC),
            end.astimezone(UTC),
            start.astimezone(UTC),
            end.astimezone(UTC),
            start.astimezone(UTC),
            end.astimezone(UTC),
        ),
    ).fetchone()
    row["followup_completion_rate"] = (
        round(100 * row["followups_completed"] / row["followups_due"], 1)
        if row["followups_due"]
        else 0
    )
    row["visit_attendance_rate"] = (
        round(100 * row["visits_attended"] / row["visits_resolved"], 1)
        if row["visits_resolved"]
        else 0
    )
    row["visit_scheduling_rate"] = (
        round(100 * row["opportunities_with_visit"] / row["opportunities_created"], 1)
        if row["opportunities_created"]
        else 0
    )
    row["negotiation_rate"] = (
        round(100 * row["opportunities_negotiating"] / row["opportunities_with_visit"], 1)
        if row["opportunities_with_visit"]
        else 0
    )
    return row
