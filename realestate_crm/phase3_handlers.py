"""Telegram workflows for Phase 3 sales conversion."""

from __future__ import annotations

import html
import json
import logging
import re
import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg import Connection
from telegram import InlineKeyboardMarkup, Update

from realestate_crm.config import Settings
from realestate_crm.models import (
    NegotiationInput,
    ReservationInput,
    SaleInput,
    VisitInput,
)
from realestate_crm.services import ai, crm, intelligence, pipeline
from realestate_crm.telegram_ui import button, main_menu
from realestate_crm.timeutils import local_label

logger = logging.getLogger(__name__)


def _token() -> str:
    return secrets.token_urlsafe(7)


LOST_LABELS = {
    "PRICE_TOO_HIGH": "Price too high",
    "DOWN_PAYMENT_TOO_HIGH": "Down payment too high",
    "PAYMENT_PLAN_UNSUITABLE": "Payment plan unsuitable",
    "PROPERTY_LOCATION": "Location",
    "PROPERTY_TYPE_MISMATCH": "Property type mismatch",
    "PROPERTY_SIZE_MISMATCH": "Property size mismatch",
    "BUYER_NOT_READY": "Buyer not ready",
    "BUYER_PURCHASED_ELSEWHERE": "Purchased elsewhere",
    "BUYER_UNRESPONSIVE": "Unresponsive",
    "TRUST_OR_DOCUMENTATION_CONCERN": "Trust or documentation concern",
    "PROPERTY_UNAVAILABLE": "Property unavailable",
    "COMPETITOR_SELECTED": "Competitor selected",
    "DUPLICATE_OPPORTUNITY": "Duplicate opportunity",
    "OTHER": "Other",
    "UNKNOWN": "Unknown",
    "UNCONFIRMED": "Unconfirmed",
}

FOLLOWUP_TYPES = (
    "CALL_BACK",
    "SEND_PROPERTY_INFORMATION",
    "SEND_PAYMENT_PLAN",
    "ARRANGE_PROPERTY_VISIT",
    "CONFIRM_PROPERTY_VISIT",
    "FOLLOW_UP_AFTER_VISIT",
    "NEGOTIATION_FOLLOW_UP",
    "BOOKING_FOLLOW_UP",
    "DOCUMENT_COLLECTION",
    "GENERAL_FOLLOW_UP",
    "CUSTOM",
)

NEGOTIATION_STAGE_OPTIONS = (
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
)

VISIT_OUTCOMES = (
    "STRONG_INTEREST",
    "INTERESTED_WITH_CONCERNS",
    "WANTS_ALTERNATIVES",
    "NEEDS_FAMILY_DISCUSSION",
    "PRICE_NEGOTIATION",
    "NOT_INTERESTED",
    "DID_NOT_ATTEND",
    "RESCHEDULE_REQUIRED",
    "OTHER",
)


def _send(chat_id: int, text: str, markup=None, parse_mode: str | None = "HTML") -> dict[str, Any]:
    return {
        "type": "send",
        "chat_id": chat_id,
        "text": text,
        "markup": markup,
        "parse_mode": parse_mode,
    }


def _edit(update: Update, actions: list[dict[str, Any]], text: str, markup=None) -> None:
    message = update.effective_message
    if update.callback_query and message:
        actions.append(
            {
                "type": "edit",
                "chat_id": message.chat_id,
                "message_id": message.message_id,
                "text": text,
                "markup": markup,
                "parse_mode": "HTML",
            }
        )
    elif message:
        actions.append(_send(message.chat_id, text, markup))


def _set_state(
    conn: Connection,
    user_id: int,
    purpose: str,
    *,
    contact_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """insert into user_input_state (telegram_user_id,purpose,contact_id,payload,expires_at)
           values (%s,%s,%s,%s,now()+interval '30 minutes')
           on conflict (telegram_user_id) do update set purpose=excluded.purpose,
             contact_id=excluded.contact_id,payload=excluded.payload,
             expires_at=excluded.expires_at,updated_at=now()""",
        (user_id, purpose, contact_id, json.dumps(payload or {}, ensure_ascii=False, default=str)),
    )


def _clear_state(conn: Connection, user_id: int) -> None:
    conn.execute("delete from user_input_state where telegram_user_id=%s", (user_id,))


def _state(conn: Connection, user_id: int) -> dict[str, Any] | None:
    return conn.execute(
        "select * from user_input_state where telegram_user_id=%s and expires_at>now()",
        (user_id,),
    ).fetchone()


def _phone(value: str | None) -> str:
    return html.escape(value or "Phone unavailable")


def _pipeline_home(conn: Connection, update: Update, actions: list[dict[str, Any]]) -> None:
    stages = pipeline.list_stages(conn)
    counts = conn.execute(
        "select current_stage,count(*) count from sales_opportunities where final_outcome is null group by current_stage"
    ).fetchall()
    count_by_stage = {row["current_stage"]: row["count"] for row in counts}
    rows = [
        [
            button(
                f"{row['display_name']} ({count_by_stage.get(row['stage_code'], 0)})",
                f"p3:list:{row['stage_code']}:0",
            )
        ]
        for row in stages
        if row["stage_code"] not in {"WON", "LOST"}
    ]
    rows.extend(
        [
            [
                button("Won sales", "p3:closed:WON:0"),
                button("Lost opportunities", "p3:closed:LOST:0"),
            ],
            [button("Search opportunities", "p3:search")],
            [button("Create opportunity", "p3:create")],
            [button("Back", "m:home")],
        ]
    )
    _edit(
        update,
        actions,
        "<b>Sales Pipeline</b>\nChoose a stage or action:",
        InlineKeyboardMarkup(rows),
    )


def _command_center(
    conn: Connection, update: Update, actions: list[dict[str, Any]], user_id: int, timezone: str
) -> None:
    now = datetime.now(UTC)
    metrics = pipeline.command_center_metrics(conn, now, timezone)
    priorities = pipeline.priority_queue(conn, user_id, 8)
    pipeline_values = metrics.get("estimated_pipeline_by_currency") or {}
    if isinstance(pipeline_values, str):
        pipeline_values = json.loads(pipeline_values)
    pipeline_text = (
        " · ".join(
            f"{currency} {amount:,.2f}" for currency, amount in sorted(pipeline_values.items())
        )
        or "Not recorded"
    )
    previous = metrics["previous_day"]
    lines = [
        f"<b>Sales Command Center · {datetime.now(ZoneInfo(timezone)).strftime('%d %b %Y')}</b>",
        "",
        f"Calls logged: <b>{metrics['calls_logged']}</b> · Answered: <b>{metrics['answered']}</b>",
        f"New qualified buyers: <b>{metrics['qualified']}</b>",
        f"Follow-ups completed: <b>{metrics['followups_completed']}</b> · Overdue: <b>{metrics['followups_overdue']}</b>",
        f"Visits today: <b>{metrics['visits_scheduled']}</b> scheduled · <b>{metrics['visits_completed']}</b> completed",
        f"Active negotiations: <b>{metrics['active_negotiations']}</b> · Reservations recorded: <b>{metrics['reservations_recorded']}</b>",
        f"Sales recorded: <b>{metrics['sales_recorded']}</b> · Opportunities won/lost: <b>{metrics['opportunities_won']}/{metrics['opportunities_lost']}</b>",
        f"Estimated pipeline: <b>{html.escape(pipeline_text)}</b>",
        f"Opportunities without a next action: <b>{metrics['without_next_action']}</b>",
        f"Previous local day: {previous['calls_logged']} calls · {previous['followups_completed']} follow-ups · {previous['sales_recorded']} sales",
        "",
        "<b>Today's priorities</b>",
    ]
    rows = []
    for item in priorities:
        lines.append(
            f"• {html.escape(item['name'])} · {html.escape(item['action'])} · {html.escape(item['priority_code'].replace('_', ' ').title())}"
        )
        target = (
            f"p3:op:{item['opportunity_id']}"
            if item.get("opportunity_id")
            else f"lead:{item['contact_id']}"
        )
        rows.append([button(f"Open {item['name'][:25]}", target)])
    if not priorities:
        lines.append("No high-priority action is due right now.")
    buttons = [
        [button("Today's Follow-Ups", "p3:followups")],
        [button("Today's Property Visits", "p3:visits")],
        [button("Active Negotiations", "p3:negs")],
        [button("Pending Reservations", "p3:reservations")],
        [button("Expected Closings", "p3:closing")],
        [
            button("Recent Sales", "p3:recent_sales"),
            button("Lost Opportunities", "p3:closed:LOST:0"),
        ],
        [button("Reactivation Queue", "p3:react:0"), button("Sales Performance", "p3:report")],
        [button("Commission Rules & Records", "p3:commissions")],
        [button("Priority Weights", "p3:weights"), button("Notification Settings", "p3:notify")],
        [button("Back", "m:home")],
    ]
    _edit(update, actions, "\n".join(lines), InlineKeyboardMarkup(rows + buttons))


def _opportunity_card(
    conn: Connection,
    update: Update,
    actions: list[dict[str, Any]],
    opportunity_id: UUID,
    timezone: str,
) -> None:
    row = pipeline.get_opportunity(conn, opportunity_id)
    if not row:
        _edit(update, actions, "This opportunity is no longer available.", main_menu())
        return
    value = (
        f"{row['currency'].strip()} {row['estimated_sale_value']:,.2f}"
        if row.get("estimated_sale_value") is not None
        else "Not recorded"
    )
    property_label = html.escape(row.get("property_title") or "No primary property")
    due = local_label(row.get("next_action_due"), timezone)
    last = local_label(row.get("last_interaction_at"), timezone)
    text = (
        f"<b>{html.escape(row['contact_name'])}</b> · {html.escape(row['stage_name'])}\n"
        f"{_phone(row.get('phone_number'))}\n"
        f"Estimated value: {html.escape(value)}\nProperty: {property_label}\n"
        f"Priority: {html.escape(str(row.get('priority_category') or 'Unscored'))}"
        f" · Lead score: {row.get('lead_score') if row.get('lead_score') is not None else '—'}\n"
        f"Last interaction: {html.escape(last)}\nNext action: {html.escape(row.get('next_action') or 'None recorded')}"
        f" · Due: {html.escape(due)}"
    )
    if row.get("final_outcome") == "LOST":
        rows = [
            [button("Reopen Lost Opportunity", f"p3:reopen:{opportunity_id}")],
            [button("View Timeline", f"p3:timeline:{opportunity_id}")],
            [button("Open Contact", f"lead:{row['contact_id']}")],
            [button("Pipeline", "p3:home")],
        ]
    elif row.get("final_outcome") == "WON":
        sale = conn.execute(
            "select id,verification_status,recorded_sale_price,currency from sale_records where opportunity_id=%s and verification_status<>'CANCELLED' order by created_at desc limit 1",
            (opportunity_id,),
        ).fetchone()
        text += (
            f"\nSale record: {html.escape(sale['verification_status'].replace('_', ' ').title())}"
            if sale
            else "\nSale record unavailable; review the audit history."
        )
        rows = [[button("View Timeline", f"p3:timeline:{opportunity_id}")]]
        if sale and sale["verification_status"] == "PENDING_VERIFICATION":
            rows.append([button("Review Recorded Sale", f"p3:saleview:{sale['id']}")])
        rows.extend(
            [[button("Open Contact", f"lead:{row['contact_id']}")], [button("Pipeline", "p3:home")]]
        )
    else:
        rows = [
            [
                button("View Timeline", f"p3:timeline:{opportunity_id}"),
                button("Update Stage", f"p3:stages:{opportunity_id}"),
            ],
            [
                button("Add Note", f"p3:note:{opportunity_id}"),
                button("Schedule Follow-Up", f"p3:follow:{opportunity_id}"),
            ],
            [button("Match Properties", f"p3:matches:{opportunity_id}")],
            [button("Record Visit", f"p3:visitnew:{opportunity_id}")],
            [
                button("Negotiation", f"p3:neg:{opportunity_id}"),
                button("Reservation", f"p3:reserve:{opportunity_id}"),
            ],
            [
                button("Record Sale", f"p3:sale:{opportunity_id}"),
                button("Close as Lost", f"p3:lost:{opportunity_id}"),
            ],
            [button("AI Summary & Next Action", f"p3:ai:{opportunity_id}")],
            [button("Open Contact", f"lead:{row['contact_id']}"), button("Pipeline", "p3:home")],
        ]
    _edit(update, actions, text, InlineKeyboardMarkup(rows))


def _visit_save_prompt(
    update: Update, actions: list[dict[str, Any]], payload: dict[str, Any]
) -> None:
    details = payload.get("details") or {}
    summary = (
        f"Time: {html.escape(str(payload['scheduled_at']))}\n"
        f"Meeting point: {html.escape(details.get('meeting_point') or 'Not recorded')}\n"
        f"Internal note: {html.escape(details.get('internal_note') or 'Not recorded')}\n"
        f"Buyer instructions: {html.escape(details.get('buyer_instructions') or 'Not recorded')}"
    )
    _edit(
        update,
        actions,
        "Review the visit details before saving:\n" + summary,
        InlineKeyboardMarkup(
            [
                [button("Confirm Visit Details", f"p3:vsave:{payload['confirm_token']}")],
                [button("Cancel", "p3:cancel")],
            ]
        ),
    )


def _create_visit_from_state(
    conn: Connection,
    update: Update,
    user_id: int,
    update_id: int,
    actions: list[dict[str, Any]],
    timezone: str,
    *,
    duplicate_confirmed: bool = False,
    confirm_token: str,
) -> None:
    state = _state(conn, user_id)
    if not state or state["purpose"] not in {"P3_VISIT_SAVE", "P3_VISIT_DUPLICATE"}:
        raise ValueError("Visit form expired")
    payload = state.get("payload") or {}
    if payload.get("confirm_token") != confirm_token:
        raise ValueError("Visit confirmation expired")
    if duplicate_confirmed != (state["purpose"] == "P3_VISIT_DUPLICATE"):
        raise ValueError("Visit confirmation expired")
    details = payload.get("details") or {}
    values = VisitInput(
        scheduled_at=payload["scheduled_at"],
        meeting_point=details.get("meeting_point"),
        internal_note=details.get("internal_note"),
        buyer_instructions=details.get("buyer_instructions"),
        availability_confirmed=bool(payload.get("availability_confirmed")),
        duplicate_confirmed=duplicate_confirmed,
    )
    try:
        with conn.transaction():
            if payload.get("visit_id"):
                pipeline.update_visit(
                    conn,
                    UUID(payload["visit_id"]),
                    "RESCHEDULE",
                    user_id,
                    update_id,
                    confirmed=True,
                )
            visit = pipeline.create_visit(
                conn,
                UUID(payload["opportunity_id"]),
                UUID(payload["property_id"]),
                values,
                user_id,
                update_id,
            )
    except ValueError as exc:
        if "active visit already exists" not in str(exc):
            raise
        _set_state(conn, user_id, "P3_VISIT_DUPLICATE", payload=payload)
        _edit(
            update,
            actions,
            "An active visit already exists for this opportunity at that time. Confirm only if this additional visit is intentional.",
            InlineKeyboardMarkup(
                [
                    [
                        button(
                            "Confirm Intentional Duplicate", f"p3:vdup:{payload['confirm_token']}"
                        )
                    ],
                    [button("Cancel", "p3:cancel")],
                ]
            ),
        )
        return
    _clear_state(conn, user_id)
    _visit_card(conn, update, actions, visit["id"], timezone)


def _opportunity_page(
    conn: Connection,
    update: Update,
    actions: list[dict[str, Any]],
    stage: str,
    page: int,
    *,
    closed: bool = False,
    query: str | None = None,
) -> None:
    rows = pipeline.list_opportunities(
        conn,
        stage=stage,
        query=query,
        page=page,
        page_size=8,
        closed=True if closed else False,
    )
    title = LOST_LABELS.get(stage, stage.replace("_", " ").title())
    buttons = []
    for item in rows:
        value = (
            f" · {item['currency'].strip()} {item['estimated_sale_value']:,.0f}"
            if item.get("estimated_sale_value") is not None
            else ""
        )
        buttons.append(
            [
                button(
                    f"{item['name'][:24]} · {item['stage_name']}{value}"[:60], f"p3:op:{item['id']}"
                )
            ]
        )
    if page > 0:
        buttons.append(
            [button("Previous", f"p3:{'closed' if closed else 'list'}:{stage}:{page - 1}")]
        )
    if len(rows) == 8:
        buttons.append([button("Next", f"p3:{'closed' if closed else 'list'}:{stage}:{page + 1}")])
    buttons.append([button("Back to Pipeline", "p3:home")])
    _edit(
        update,
        actions,
        f"<b>{html.escape(title)}</b> · page {page + 1}\nSelect an opportunity:",
        InlineKeyboardMarkup(buttons),
    )


def _visit_card(
    conn: Connection, update: Update, actions: list[dict[str, Any]], visit_id: UUID, timezone: str
) -> None:
    row = pipeline.get_visit(conn, visit_id)
    if not row:
        _edit(update, actions, "This visit is no longer available.", main_menu())
        return
    text = (
        f"<b>{html.escape(row['contact_name'])}</b> · {html.escape(row['property_title'])}\n"
        f"When: {html.escape(local_label(row['scheduled_at'], timezone))}\n"
        f"Confirmation: <b>{html.escape(row['confirmation_status'].replace('_', ' ').title())}</b>\n"
        f"Visit status: <b>{html.escape(row['visit_status'].replace('_', ' ').title())}</b>\n"
        f"Meeting point: {html.escape(row.get('meeting_point') or 'Not recorded')}"
    )
    rows = []
    if row["confirmation_status"] != "CONFIRMED" and row["visit_status"] == "SCHEDULED":
        rows.append([button("Confirm Visit", f"p3:vc:{visit_id}")])
    if row["visit_status"] in {"SCHEDULED", "BUYER_ARRIVED"}:
        rows.extend(
            [
                [button("Mark Buyer Arrived", f"p3:va:{visit_id}")],
                [button("Mark Completed", f"p3:vd:{visit_id}")],
                [button("Buyer Did Not Attend", f"p3:vn:{visit_id}")],
                [
                    button("Reschedule", f"p3:vr:{visit_id}"),
                    button("Cancel Visit", f"p3:vx:{visit_id}"),
                ],
            ]
        )
    if row["visit_status"] in {"COMPLETED", "DID_NOT_ATTEND"}:
        rows.append([button("Record Feedback", f"p3:vf:{visit_id}")])
    rows.extend(
        [
            [button("Visit Timeline", f"p3:vtimeline:{visit_id}")],
            [button("Open Opportunity", f"p3:op:{row['opportunity_id']}")],
            [button("Visits", "p3:visits")],
        ]
    )
    _edit(update, actions, text, InlineKeyboardMarkup(rows))


def _visit_page(
    conn: Connection, update: Update, actions: list[dict[str, Any]], timezone: str
) -> None:
    sections = pipeline.command_center_sections(conn, datetime.now(UTC), 20, timezone)
    rows = []
    for visit in sections["visits"]:
        rows.append(
            [
                button(
                    f"{visit['name'][:22]} · {local_label(visit['scheduled_at'], timezone)}",
                    f"p3:v:{visit['id']}",
                )
            ]
        )
    rows.extend([[button("Back", "p3:command")], [button("Main Menu", "m:home")]])
    _edit(
        update,
        actions,
        "<b>Today's Property Visits</b>\nChoose a visit:",
        InlineKeyboardMarkup(rows),
    )


def _followup_page(
    conn: Connection, update: Update, actions: list[dict[str, Any]], timezone: str
) -> None:
    rows = conn.execute(
        """select f.id,f.opportunity_id,f.contact_id,f.follow_up_type,f.reason,f.scheduled_at,c.name
           from follow_ups f join contacts c on c.id=f.contact_id where f.status='PENDING'
             and f.scheduled_at<=now()+interval '1 day' order by f.scheduled_at,f.priority desc,f.id limit 20"""
    ).fetchall()
    buttons = [
        [
            button(
                f"{row['name'][:24]} · {local_label(row['scheduled_at'], timezone)}",
                f"p3:fd:{row['id']}",
            )
        ]
        for row in rows
    ]
    buttons.extend([[button("Back", "p3:command")], [button("Main Menu", "m:home")]])
    _edit(
        update,
        actions,
        "<b>Today's Follow-Ups</b>\nChoose an action to complete or reschedule:",
        InlineKeyboardMarkup(buttons),
    )


def _datetime_local(value: str, timezone: str) -> datetime:
    try:
        naive = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("Use YYYY-MM-DD HH:MM in the displayed timezone") from exc
    zone = ZoneInfo(timezone)
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError(
            "This local time is ambiguous because of a timezone change. Enter a different time"
        )
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive:
        raise ValueError("That local time does not exist because of a timezone change")
    return first.astimezone(UTC)


def _quick_time(code: str, timezone: str) -> datetime:
    now = datetime.now(ZoneInfo(timezone))
    if code == "30m":
        target = now + timedelta(minutes=30)
    elif code == "1h":
        target = now + timedelta(hours=1)
    elif code == "today":
        target = min(
            now + timedelta(hours=2), now.replace(hour=23, minute=0, second=0, microsecond=0)
        )
    elif code == "tomorrow":
        target = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    elif code in {"3d", "7d", "14d"}:
        days = int(code[:-1])
        target = (now + timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
    else:
        raise ValueError("Unknown follow-up time")
    if target <= now:
        target = now + timedelta(minutes=30)
    return target.astimezone(UTC)


def _start_followup(
    conn: Connection,
    update: Update,
    user_id: int,
    actions: list[dict[str, Any]],
    opportunity_id: UUID,
) -> None:
    rows = [
        [button(kind.replace("_", " ").title(), f"p3:ft:{opportunity_id}:{index}")]
        for index, kind in enumerate(FOLLOWUP_TYPES)
    ]
    rows.append([button("Cancel", "p3:cancel")])
    _set_state(conn, user_id, "P3_FOLLOWUP_TYPE", payload={"opportunity_id": str(opportunity_id)})
    _edit(
        update,
        actions,
        "Choose the follow-up action. You can add a reason or note in the next step.",
        InlineKeyboardMarkup(rows),
    )


def _followup_time_markup() -> InlineKeyboardMarkup:
    rows = [
        [button(label, f"p3:ftime:{code}")]
        for label, code in (
            ("In 30 minutes", "30m"),
            ("In 1 hour", "1h"),
            ("Later today", "today"),
            ("Tomorrow", "tomorrow"),
            ("In 3 days", "3d"),
            ("In 7 days", "7d"),
            ("In 14 days", "14d"),
            ("Choose date and time", "custom"),
        )
    ]
    rows.append([button("Cancel", "p3:cancel")])
    return InlineKeyboardMarkup(rows)


def _opportunity_matches(
    conn: Connection, update: Update, actions: list[dict[str, Any]], opportunity_id: UUID
) -> None:
    opportunity = pipeline.get_opportunity(conn, opportunity_id)
    if not opportunity:
        _edit(update, actions, "Opportunity not found.", main_menu())
        return
    matches = intelligence.find_matching_properties(conn, opportunity["contact_id"], limit=8)
    if not matches:
        _edit(
            update,
            actions,
            "No available property currently matches this buyer's recorded profile.",
            InlineKeyboardMarkup(
                [
                    [button("Open Buyer Profile", f"lead:{opportunity['contact_id']}")],
                    [button("Back", f"p3:op:{opportunity_id}")],
                ]
            ),
        )
        return
    select_token = _token()
    property_ids = [str(item["property"]["id"]) for item in matches]
    _set_state(
        conn,
        update.effective_user.id,
        "P3_PICK_VISIT_PROPERTY",
        payload={
            "opportunity_id": str(opportunity_id),
            "property_ids": property_ids,
            "select_token": select_token,
        },
    )
    rows = []
    for item in matches:
        prop = item["property"]
        match = item["match"]
        label = f"{prop['title'][:25]} · {match['score']} pts"
        rows.append([button(label[:60], f"p3:pickprop:{prop['id']}:{select_token}")])
    rows.extend([[button("Back", f"p3:op:{opportunity_id}")], [button("Main Menu", "m:home")]])
    _edit(
        update,
        actions,
        "<b>Available buyer matches</b>\nSelect a property to link to this opportunity:",
        InlineKeyboardMarkup(rows),
    )


def _set_property_link(
    conn: Connection, opportunity_id: UUID, property_id: UUID, actor_id: int, update_id: int
) -> None:
    old = conn.execute(
        "select contact_id,primary_property_id,currency,final_outcome from sales_opportunities where id=%s for update",
        (opportunity_id,),
    ).fetchone()
    prop = conn.execute(
        "select id,currency,availability_status from properties where id=%s", (property_id,)
    ).fetchone()
    if not old or not prop:
        raise ValueError("Opportunity or property is no longer available")
    if old["final_outcome"]:
        raise ValueError("Cannot change a closed opportunity")
    if prop["availability_status"] != "AVAILABLE":
        raise ValueError("This property is not currently recorded as available")
    if old["currency"].strip() != prop["currency"].strip():
        raise ValueError("Property currency must match the opportunity currency")
    conn.execute(
        "update sales_opportunities set primary_property_id=%s,updated_at=now(),last_interaction_at=now() where id=%s",
        (property_id, opportunity_id),
    )
    pipeline._opportunity_event(
        conn,
        opportunity_id=opportunity_id,
        event_type="PROPERTY_LINKED",
        actor_id=actor_id,
        update_id=update_id,
        details={"property_id": property_id},
    )
    pipeline._record_audit(
        conn,
        actor_id=actor_id,
        event_type="OPPORTUNITY_PROPERTY_LINKED",
        entity_type="OPPORTUNITY",
        entity_id=opportunity_id,
        contact_id=old["contact_id"],
        old_value={"property_id": old["primary_property_id"]},
        new_value={"property_id": property_id},
        request_key=f"{actor_id}:{update_id}",
    )


def _reservation_list(conn: Connection, update: Update, actions: list[dict[str, Any]]) -> None:
    rows = conn.execute(
        """select r.id,r.reservation_status,r.payment_status,r.reservation_amount,r.currency,
                  c.name,p.title from reservations r join sales_opportunities o on o.id=r.opportunity_id
           join contacts c on c.id=o.contact_id join properties p on p.id=r.property_id
           where r.reservation_status not in ('CANCELLED','EXPIRED') order by r.expires_at nulls last,r.created_at desc limit 20"""
    ).fetchall()
    buttons = [
        [
            button(
                f"{r['name'][:18]} · {r['reservation_status'].replace('_', ' ').title()}",
                f"p3:resv:{r['id']}",
            )
        ]
        for r in rows
    ]
    buttons.extend([[button("Back", "p3:command")], [button("Main Menu", "m:home")]])
    _edit(
        update,
        actions,
        "<b>Pending Reservations</b>\nSelect a record:",
        InlineKeyboardMarkup(buttons),
    )


def _reservation_card(
    conn: Connection, update: Update, actions: list[dict[str, Any]], reservation_id: UUID
) -> None:
    row = conn.execute(
        """select r.*,c.name,p.title property_title from reservations r
           join sales_opportunities o on o.id=r.opportunity_id join contacts c on c.id=o.contact_id
           join properties p on p.id=r.property_id where r.id=%s""",
        (reservation_id,),
    ).fetchone()
    if not row:
        _edit(update, actions, "Reservation not found.", main_menu())
        return
    text = (
        f"<b>{html.escape(row['name'])}</b> · {html.escape(row['property_title'])}\n"
        f"Recorded amount: {row['currency'].strip()} {row['reservation_amount']:,.2f}\n"
        f"Reservation status: <b>{row['reservation_status'].replace('_', ' ').title()}</b>\n"
        f"Payment status: <b>{row['payment_status'].replace('_', ' ').title()}</b>\n"
        f"Reference: {html.escape(row.get('reservation_reference') or 'Not recorded')}"
    )
    rows = []
    if row["reservation_status"] in {"REQUESTED", "AWAITING_CONFIRMATION"}:
        rows.append([button("Record Confirmation", f"p3:rc:{reservation_id}")])
    if row["reservation_status"] not in {"CANCELLED", "EXPIRED"}:
        rows.extend(
            [
                [button("Set Payment Pending", f"p3:rp:{reservation_id}:PENDING")],
                [button("Set Partially Paid", f"p3:rp:{reservation_id}:PARTIALLY_PAID")],
                [button("Set Paid", f"p3:rp:{reservation_id}:PAID")],
                [button("Cancel Reservation", f"p3:rx:{reservation_id}")],
            ]
        )
    rows.extend([[button("Command Center", "p3:command")], [button("Main Menu", "m:home")]])
    _edit(update, actions, text, InlineKeyboardMarkup(rows))


def _sale_card(
    conn: Connection, update: Update, actions: list[dict[str, Any]], sale_id: UUID
) -> None:
    row = conn.execute(
        """select s.*,c.name,p.title property_title from sale_records s
           join contacts c on c.id=s.buyer_contact_id
           join properties p on p.id=s.property_id where s.id=%s""",
        (sale_id,),
    ).fetchone()
    if not row:
        _edit(update, actions, "Sale record not found.", main_menu())
        return
    text = (
        f"<b>{html.escape(row['name'])}</b> · {html.escape(row['property_title'])}\n"
        f"Recorded price: {row['currency'].strip()} {row['recorded_sale_price']:,.2f}\n"
        f"Sale date: {row['sale_date']}\n"
        f"Verification: <b>{row['verification_status'].replace('_', ' ').title()}</b>\n"
        f"Reference: {html.escape(row.get('supporting_reference') or 'Not recorded')}"
    )
    buttons = []
    status = row["verification_status"]
    if status in {"PENDING_VERIFICATION", "DISPUTED"}:
        buttons.append([button("Confirm Verification", f"p3:verify:{sale_id}")])
    if status in {"PENDING_VERIFICATION", "VERIFIED"}:
        buttons.append([button("Mark Disputed", f"p3:salestatus:{sale_id}:DISPUTED")])
    if status in {"PENDING_VERIFICATION", "DISPUTED"}:
        buttons.append([button("Cancel Sale Record", f"p3:salestatus:{sale_id}:CANCELLED")])
    buttons.extend(
        [
            [button("Opportunity", f"p3:op:{row['opportunity_id']}")],
            [button("Recent Sales", "p3:recent_sales")],
            [button("Command Center", "p3:command")],
        ]
    )
    _edit(update, actions, text, InlineKeyboardMarkup(buttons))


def _sale_status_prompt(
    conn: Connection,
    update: Update,
    user_id: int,
    actions: list[dict[str, Any]],
    sale_id: UUID,
    status: str,
) -> None:
    allowed = {"VERIFIED", "DISPUTED", "CANCELLED"}
    if status not in allowed:
        raise ValueError("Choose a supported sale verification status")
    row = conn.execute(
        "select verification_status from sale_records where id=%s", (sale_id,)
    ).fetchone()
    if not row:
        raise ValueError("Sale record was not found")
    current = row["verification_status"]
    transitions = {
        "PENDING_VERIFICATION": {"VERIFIED", "DISPUTED", "CANCELLED"},
        "DISPUTED": {"VERIFIED", "CANCELLED"},
        "VERIFIED": {"DISPUTED"},
    }
    if status not in transitions.get(current, set()):
        raise ValueError("This sale record cannot be changed to that status")
    token = _token()
    _set_state(
        conn,
        user_id,
        "P3_SALE_STATUS",
        payload={"sale_id": str(sale_id), "status": status, "confirm_token": token},
    )
    prompt = {
        "VERIFIED": "Confirm that the sale details were checked against an authorized source?",
        "DISPUTED": "Confirm recording that this sale is disputed and needs review?",
        "CANCELLED": "Confirm cancellation of this unverified sale record?",
    }[status]
    _edit(
        update,
        actions,
        prompt,
        InlineKeyboardMarkup(
            [
                [button("Confirm Status", f"p3:syes:{sale_id}:{status}:{token}")],
                [button("Back", f"p3:saleview:{sale_id}")],
            ]
        ),
    )


def _commission_menu(conn: Connection, update: Update, actions: list[dict[str, Any]]) -> None:
    rules = conn.execute(
        """select id,rule_name,rule_type,amount_or_rate,currency,project_name,
                  agent_telegram_user_id,active from commission_rules
           order by active desc,created_at desc,id desc limit 15"""
    ).fetchall()
    records = conn.execute(
        """select cr.id,cr.commission_status,cr.calculated_amount,cr.currency,c.name
           from commission_records cr join sale_records s on s.id=cr.sale_record_id
           join contacts c on c.id=s.buyer_contact_id
           where cr.commission_status<>'CANCELLED' order by cr.updated_at desc,cr.id desc limit 8"""
    ).fetchall()
    lines = ["<b>Commission Rules & Records</b>"]
    rows = []
    for rule in rules:
        kind_value = (
            f"{rule['amount_or_rate']:,.4f}%"
            if rule["rule_type"] == "PERCENTAGE"
            else f"{rule['currency'].strip()} {rule['amount_or_rate']:,.2f}"
        )
        scope = rule.get("project_name") or "All projects"
        if rule.get("agent_telegram_user_id"):
            scope += f" · Agent {rule['agent_telegram_user_id']}"
        lines.append(
            f"• {html.escape(rule['rule_name'])}: {html.escape(kind_value)} · "
            f"{html.escape(scope)} · {'Active' if rule['active'] else 'Inactive'}"
        )
        rows.append([button(f"Rule · {rule['rule_name'][:34]}", f"p3:rule:{rule['id']}")])
    if not rules:
        lines.append("No commission rules are configured.")
    lines.append("\nRecent commission records:")
    for record in records:
        rows.append(
            [
                button(
                    f"{record['name'][:20]} · {record['currency'].strip()} {record['calculated_amount']:,.0f} · {record['commission_status'].title()}",
                    f"p3:commission:{record['id']}",
                )
            ]
        )
    if not records:
        lines.append("No commission amounts are recorded yet.")
    rows.extend(
        [
            [button("Add Commission Rule", "p3:crnew")],
            [button("All Commission Records & Sales", "p3:commlist:0")],
            [button("Command Center", "p3:command")],
        ]
    )
    _edit(update, actions, "\n".join(lines), InlineKeyboardMarkup(rows))


def _commission_list(
    conn: Connection, update: Update, actions: list[dict[str, Any]], page: int, user_id: int
) -> None:
    page = max(0, min(page, 100))
    offset = page * 10
    actor = conn.execute(
        "select role from crm_users where telegram_user_id=%s", (user_id,)
    ).fetchone()
    own_only = bool(actor and actor["role"] == "SALESPERSON")
    records = conn.execute(
        """select cr.id,s.id sale_id,c.name,cr.commission_status,cr.calculated_amount,cr.currency
           from commission_records cr join sale_records s on s.id=cr.sale_record_id
           join contacts c on c.id=s.buyer_contact_id
           where cr.commission_status<>'CANCELLED'
             and (not %s or coalesce(cr.responsible_telegram_user_id,s.responsible_telegram_user_id)=%s)
           order by cr.updated_at desc,cr.id desc
           limit 10 offset %s""",
        (own_only, user_id, offset),
    ).fetchall()
    missing = conn.execute(
        """select s.id,c.name,s.recorded_sale_price,s.currency,s.verification_status
           from sale_records s join contacts c on c.id=s.buyer_contact_id
           left join commission_records cr on cr.sale_record_id=s.id
           where cr.id is null and s.verification_status<>'CANCELLED'
             and (not %s or s.responsible_telegram_user_id=%s)
           order by s.created_at desc,s.id desc limit 10 offset %s""",
        (own_only, user_id, offset),
    ).fetchall()
    rows = []
    for record in records:
        rows.append(
            [
                button(
                    f"{record['name'][:18]} · {record['currency'].strip()} {record['calculated_amount']:,.0f} · {record['commission_status'].title()}",
                    f"p3:commission:{record['id']}",
                )
            ]
        )
    for sale in missing:
        rows.append(
            [
                button(
                    f"Manual amount · {sale['name'][:18]} · {sale['currency'].strip()} {sale['recorded_sale_price']:,.0f}",
                    f"p3:cmnew:{sale['id']}",
                )
            ]
        )
    if page:
        rows.append([button("Previous", f"p3:commlist:{page - 1}")])
    if len(records) == 10 or len(missing) == 10:
        rows.append([button("Next", f"p3:commlist:{page + 1}")])
    rows.extend(
        [[button("Commission Rules", "p3:commissions")], [button("Command Center", "p3:command")]]
    )
    _edit(
        update,
        actions,
        "<b>Commission Records</b>\nRecorded commissions and sales awaiting a manual commission amount:",
        InlineKeyboardMarkup(rows),
    )


def _commission_card(
    conn: Connection,
    update: Update,
    actions: list[dict[str, Any]],
    commission_id: UUID,
    user_id: int,
) -> None:
    row = conn.execute(
        """select cr.*,s.recorded_sale_price,s.verification_status sale_status,c.name,p.title property_title
           from commission_records cr join sale_records s on s.id=cr.sale_record_id
           join contacts c on c.id=s.buyer_contact_id join properties p on p.id=s.property_id
           where cr.id=%s""",
        (commission_id,),
    ).fetchone()
    if not row:
        _edit(update, actions, "Commission record not found.", main_menu())
        return
    text = (
        f"<b>{html.escape(row['name'])}</b> · {html.escape(row['property_title'])}\n"
        f"Sale verification: {row['sale_status'].replace('_', ' ').title()}\n"
        f"Recorded sale price: {row['currency'].strip()} {row['recorded_sale_price']:,.2f}\n"
        f"Commission: {row['currency'].strip()} {row['calculated_amount']:,.2f} · "
        f"{row['calculation_type'].title()} · {row['commission_status'].title()}"
    )
    approved = (
        row["approved_amount"] if row["approved_amount"] is not None else row["calculated_amount"]
    )
    text += (
        f"\nApproved {row['currency'].strip()} {approved:,.2f} · "
        f"paid {row['currency'].strip()} {row['paid_amount']:,.2f} · "
        f"outstanding {row['currency'].strip()} {max(Decimal('0'), Decimal(str(approved)) - Decimal(str(row['paid_amount']))):,.2f}"
    )
    if row["payment_reference"]:
        text += f"\nLatest payment reference: {html.escape(row['payment_reference'])}"
    actor = conn.execute(
        "select role from crm_users where telegram_user_id=%s", (user_id,)
    ).fetchone()
    can_manage = bool(actor and actor["role"] == "SUPER_ADMIN")
    rows = []
    if can_manage and row["commission_status"] in {"ESTIMATED", "CALCULATED"}:
        rows.append([button("Confirm Commission", f"p3:cmstatus:{commission_id}:CONFIRMED")])
    if can_manage and row["commission_status"] == "DISPUTED":
        rows.append([button("Resolve Dispute", f"p3:cmstatus:{commission_id}:CONFIRMED")])
    if can_manage and row["commission_status"] in {"CONFIRMED", "APPROVED", "PARTIALLY_PAID"}:
        rows.append([button("Record as Paid", f"p3:cmstatus:{commission_id}:PAID")])
    if can_manage and row["commission_status"] not in {"PAID", "CANCELLED"}:
        rows.append([button("Enter Manual Amount", f"p3:cmamount:{commission_id}")])
    if can_manage and row["commission_status"] not in {"PAID", "CANCELLED", "DISPUTED"}:
        rows.append([button("Mark Disputed", f"p3:cmstatus:{commission_id}:DISPUTED")])
    rows.extend(
        [[button("Commission Records", "p3:commlist:0")], [button("Command Center", "p3:command")]]
    )
    _edit(update, actions, text, InlineKeyboardMarkup(rows))


def _reactivation_page(
    conn: Connection, update: Update, actions: list[dict[str, Any]], page: int
) -> None:
    rows = pipeline.list_reactivation_candidates(conn, page=page, page_size=6)
    buttons = []
    for item in rows:
        buttons.append(
            [
                button(
                    f"{item['name'][:25]} · {item['rule_code'].replace('_', ' ').title()}",
                    f"p3:reactitem:{item['id']}",
                )
            ]
        )
    if page:
        buttons.append([button("Previous", f"p3:react:{page - 1}")])
    if len(rows) == 6:
        buttons.append([button("Next", f"p3:react:{page + 1}")])
    buttons.extend([[button("Refresh", "p3:react:0")], [button("Back", "p3:command")]])
    _edit(
        update,
        actions,
        "<b>Reactivation Queue</b>\nReview why each eligible buyer was selected:",
        InlineKeyboardMarkup(buttons),
    )


def _reactivation_card(
    conn: Connection, update: Update, actions: list[dict[str, Any]], recommendation_id: UUID
) -> None:
    row = conn.execute(
        """select r.*,c.name,c.phone_number,c.normalized_phone,c.contact_eligibility,c.lead_status,
                  c.conversion_state,
                  c.last_called_at,o.current_stage from reactivation_recommendations r
           join contacts c on c.id=r.contact_id left join sales_opportunities o on o.id=r.opportunity_id
           where r.id=%s and r.status='OPEN'""",
        (recommendation_id,),
    ).fetchone()
    if not row:
        _edit(
            update,
            actions,
            "This suggestion has been handled or is no longer eligible.",
            InlineKeyboardMarkup([[button("Queue", "p3:react:0")]]),
        )
        return
    facts = row.get("relevant_facts") or {}
    last = local_label(row.get("last_called_at"), "Africa/Addis_Ababa")
    text = (
        f"<b>{html.escape(row['name'])}</b>\n{_phone(row.get('phone_number'))}\n"
        f"Why selected: {html.escape(row['rationale'])}\nSuggested action: {html.escape(row['suggested_action'])}\n"
        f"Last call: {html.escape(last)}\nRecorded facts: {html.escape(json.dumps(facts, ensure_ascii=False)[:500])}"
    )
    rows = []
    call_allowed = pipeline.contact_is_callable(row)
    if call_allowed:
        rows.append([button("Call Now (open dialer)", f"p3:reactcall:{recommendation_id}")])
    if call_allowed:
        rows.append([button("Schedule Follow-Up", f"p3:reactschedule:{recommendation_id}")])
    rows.extend(
        [
            [button("Find Alternative Properties", f"p3:reactmatch:{recommendation_id}")],
            [button("Dismiss Suggestion", f"p3:reactdismiss:{recommendation_id}")],
            [button("Queue", "p3:react:0")],
        ]
    )
    _edit(update, actions, text, InlineKeyboardMarkup(rows))


def _lost_reasons(update: Update, actions: list[dict[str, Any]], opportunity_id: UUID) -> None:
    rows = []
    reasons = list(LOST_LABELS.items())
    for i in range(0, len(reasons), 2):
        pair = reasons[i : i + 2]
        rows.append(
            [
                button(label, f"p3:lr:{opportunity_id}:{reasons.index((code, label))}")
                for code, label in pair
            ]
        )
    rows.append([button("Cancel", f"p3:op:{opportunity_id}")])
    _edit(
        update,
        actions,
        "<b>Close opportunity as lost</b>\nChoose the recorded reason. Unknown is allowed.",
        InlineKeyboardMarkup(rows),
    )


def _summary_report(
    conn: Connection,
    update: Update,
    actions: list[dict[str, Any]],
    settings: Settings,
    period: str = "today",
    custom_start: date | None = None,
    custom_end: date | None = None,
) -> None:
    now = datetime.now(ZoneInfo(settings.app_timezone))
    start, end, label_start, label_end = intelligence.reporting_bounds(
        period,
        settings.app_timezone,
        now=now,
        custom_start=custom_start,
        custom_end=custom_end,
    )
    summary = pipeline.report_pipeline(conn, start, end, settings.app_timezone)
    activity = intelligence.sales_intelligence_report(
        conn,
        period,
        settings.app_timezone,
        custom_start=custom_start,
        custom_end=custom_end,
    )
    workflow = pipeline.workflow_report(conn, start, end, datetime.now(UTC))
    period_name = (
        "Last 7 days" if period == "last7" else "Today" if period == "today" else "Custom period"
    )
    currencies = (
        ", ".join(
            f"{row['currency'].strip()} {row['amount']:,.2f}" for row in summary["pipeline_values"]
        )
        or "Not recorded"
    )
    verified_sales = (
        ", ".join(
            f"{row['currency'].strip()} {row['amount']:,.2f}" for row in summary["sales_values"]
        )
        or "None"
    )
    commissions = (
        ", ".join(
            f"{row['commission_status'].title()} {row['currency'].strip()} {row['amount']:,.2f}"
            for row in summary["commission_values"]
        )
        or "None"
    )
    qualification_rate = (
        round(100 * activity["qualified"] / activity["unique_contacted"], 1)
        if activity["unique_contacted"]
        else 0
    )
    lines = [
        f"<b>Sales Performance · {period_name} ({label_start}–{label_end})</b>",
        "",
        f"Calls logged: {activity['attempted']} · Unique buyers contacted: {activity['unique_contacted']}",
        f"Answered: {activity['answered']} ({activity['answer_rate']}% of attempts)",
        f"Qualified: {activity['qualified']} ({qualification_rate}% of unique buyers contacted)",
        f"Follow-ups due/completed: {workflow['followups_due']}/{workflow['followups_completed']} · Completion: {workflow['followup_completion_rate']}%",
        f"Visits scheduled: {workflow['visits_scheduled']} · Attendance: {workflow['visits_attended']}/{workflow['visits_resolved']} resolved ({workflow['visit_attendance_rate']}%)",
        f"Negotiations started: {workflow['negotiations_started']} · Reservations recorded: {workflow['reservations_recorded']}",
        f"Sales recorded: {workflow['sales_recorded']} · Opportunities won/lost: {summary['won']}/{summary['lost']}",
        f"Active opportunities: {summary['active']} · Overdue next actions: {summary['overdue_actions']} · Without next action: {summary['without_next_action']}",
        f"Estimated pipeline by currency: {html.escape(currencies)}",
        f"Verified sale value by currency: {html.escape(verified_sales)}",
        f"Recorded commission amounts by status and currency: {html.escape(commissions)}",
        "",
        "<b>By stage</b>",
    ]
    lines.extend(
        f"{html.escape(row['display_name'])}: {row['count']}"
        for row in summary["stages"]
        if row["count"]
    )
    lines.extend(["", "<b>Lost reasons this period</b>"])
    lines.extend(
        f"{html.escape((row['lost_reason'] or 'Unknown').replace('_', ' ').title())}: {row['count']}"
        for row in summary["lost_reasons"]
    )
    lines.append(
        "\nQualification rate = qualified prospects / unique buyers contacted. Visit attendance = attended / visits with a resolved attendance outcome. Follow-up completion = completed / follow-ups due in this period. These activity ratios do not imply causation. Monetary totals remain grouped by currency."
    )
    _edit(
        update,
        actions,
        "\n".join(lines),
        InlineKeyboardMarkup(
            [[button("Command Center", "p3:command")], [button("Main Menu", "m:home")]]
        ),
    )


def handle_callback(
    conn: Connection,
    update: Update,
    user_id: int,
    settings: Settings,
    update_id: int,
    actions: list[dict[str, Any]],
    data: str,
) -> bool:
    if not data.startswith("p3:"):
        return False
    query = update.callback_query
    if query:
        actions.append({"type": "answer", "callback_id": query.id, "text": ""})
    parts = data.split(":")
    op = parts[1]
    try:
        prefs = crm.ensure_user(conn, user_id, settings)
        timezone = prefs["timezone"]
        if op in {
            "home",
            "pipeline",
            "command",
            "list",
            "closed",
            "op",
            "search",
            "followups",
            "negs",
            "reservations",
            "commissions",
            "commlist",
            "commission",
            "rule",
            "closing",
            "recent_sales",
            "report",
            "visits",
            "v",
            "vtimeline",
            "resv",
            "saleview",
            "react",
            "reactitem",
        }:
            _clear_state(conn, user_id)
        if op == "cancel":
            _clear_state(conn, user_id)
            _edit(update, actions, "Cancelled.", main_menu())
        elif op in {"home", "pipeline"}:
            _clear_state(conn, user_id)
            _pipeline_home(conn, update, actions)
        elif op == "command":
            _command_center(conn, update, actions, user_id, timezone)
        elif op == "list" and len(parts) == 4:
            _opportunity_page(conn, update, actions, parts[2], int(parts[3]))
        elif op == "closed" and len(parts) == 4:
            _opportunity_page(conn, update, actions, parts[2], int(parts[3]), closed=True)
        elif op == "create":
            _set_state(conn, user_id, "P3_OPP_CONTACT")
            _edit(
                update,
                actions,
                "Send the buyer name or phone number to start an opportunity.",
                InlineKeyboardMarkup([[button("Cancel", "p3:cancel")]]),
            )
        elif op == "search":
            _set_state(conn, user_id, "P3_OPP_SEARCH")
            _edit(
                update,
                actions,
                "Send a buyer name, phone, or property reference.",
                InlineKeyboardMarkup([[button("Cancel", "p3:cancel")]]),
            )
        elif op == "contact" and len(parts) == 4:
            state = _state(conn, user_id)
            payload = state.get("payload") or {} if state else {}
            if (
                not state
                or state["purpose"] != "P3_OPP_CONTACT_CHOOSE"
                or parts[2] not in payload.get("contact_ids", [])
                or payload.get("select_token") != parts[3]
            ):
                raise ValueError("Contact selection expired")
            contact_id = UUID(parts[2])
            row = pipeline.create_opportunity(conn, contact_id, user_id, update_id)
            _clear_state(conn, user_id)
            _opportunity_card(conn, update, actions, row["id"], timezone)
        elif op == "op" and len(parts) == 3:
            _opportunity_card(conn, update, actions, UUID(parts[2]), timezone)
        elif op == "ai" and len(parts) == 3:
            if not getattr(settings, "ai_enabled", False):
                raise ValueError(
                    "Optional AI assistance is disabled; core pipeline actions are available."
                )
            opportunity_id = UUID(parts[2])
            row = conn.execute(
                """select o.id,o.contact_id,o.primary_property_id,o.current_stage,o.final_outcome,
                          o.next_action,c.contact_eligibility,c.lead_status,c.conversion_state,
                          c.phone_number,c.normalized_phone,
                          p.title,p.project_name,p.property_type,p.location,p.price,p.currency,
                          p.availability_status,p.bedrooms,p.area_sqm,
                          i.property_type preferred_type,i.preferred_location,i.accepted_locations,
                          i.min_budget,i.max_budget,i.currency preferred_currency,i.bedrooms preferred_bedrooms,
                          i.preferred_payment_plan,i.expected_timeline,i.additional_requirements,
                          q.qualification_status,q.next_action qualification_next_action
                   from sales_opportunities o join contacts c on c.id=o.contact_id
                   left join properties p on p.id=o.primary_property_id
                   left join lateral (select * from contact_property_interests
                     where contact_id=c.id order by updated_at desc,id desc limit 1) i on true
                   left join lead_qualifications q on q.contact_id=c.id
                   where o.id=%s""",
                (opportunity_id,),
            ).fetchone()
            if not row or row["final_outcome"] is not None:
                raise ValueError("Only an open opportunity can be sent for optional AI assistance")
            if (
                row["contact_eligibility"] != "ELIGIBLE"
                or row["lead_status"]
                in {"DO_NOT_CONTACT", "NOT_INTERESTED", "DISQUALIFIED", "CONVERTED"}
                or row["conversion_state"] == "CONVERTED"
            ):
                raise ValueError("This contact is not eligible for AI assistance")
            phone_digits = re.sub(
                r"\D", "", row.get("normalized_phone") or row.get("phone_number") or ""
            )

            def redact(value: Any) -> Any:
                if isinstance(value, str):
                    cleaned = value[:700]
                    if len(phone_digits) >= 8:
                        phone_pattern = (
                            r"(?<!\d)" + r"[\s().-]*".join(map(re.escape, phone_digits)) + r"(?!\d)"
                        )
                        cleaned = re.sub(phone_pattern, "[phone removed]", cleaned)
                    return cleaned
                if isinstance(value, dict):
                    return {str(key)[:60]: redact(item) for key, item in list(value.items())[:20]}
                if isinstance(value, list):
                    return [redact(item) for item in value[:10]]
                if hasattr(value, "isoformat"):
                    return value.isoformat()
                return value

            interest = {
                key: row[key]
                for key in (
                    "preferred_type",
                    "preferred_location",
                    "accepted_locations",
                    "min_budget",
                    "max_budget",
                    "preferred_currency",
                    "preferred_bedrooms",
                    "preferred_payment_plan",
                    "expected_timeline",
                    "additional_requirements",
                    "qualification_status",
                    "qualification_next_action",
                )
                if row.get(key) is not None
            }
            property_facts = {
                key: row[key]
                for key in (
                    "title",
                    "project_name",
                    "property_type",
                    "location",
                    "price",
                    "currency",
                    "availability_status",
                    "bedrooms",
                    "area_sqm",
                )
                if row.get(key) is not None
            }
            events = pipeline.opportunity_timeline(conn, opportunity_id, limit=8)
            event_facts = [
                {
                    "event_type": event["event_type"],
                    "recorded_at": event["created_at"].isoformat(),
                    "details": redact(event.get("details") or {}),
                }
                for event in events
            ]
            try:
                job = ai.queue_ai_job(
                    conn,
                    settings=settings,
                    user_id=user_id,
                    chat_id=update.effective_chat.id,
                    job_type="OPPORTUNITY_ASSIST",
                    payload={
                        "current_stage": row["current_stage"],
                        "next_action": row.get("next_action"),
                        "buyer_requirements": redact(interest),
                        "property_summary": redact(property_facts),
                        "recent_recorded_events": redact(event_facts),
                    },
                    contact_id=row["contact_id"],
                    property_id=row["primary_property_id"],
                    opportunity_id=opportunity_id,
                )
            except ai.AIError as exc:
                raise ValueError(str(exc)) from exc
            _set_state(conn, user_id, "P3_AI_WAIT", payload={"job_id": str(job["id"])})
            _edit(
                update,
                actions,
                "Optional AI summary queued for this opportunity only. It will not change the pipeline; review any suggestion before acting.",
                InlineKeyboardMarkup([[button("Open Opportunity", f"p3:op:{opportunity_id}")]]),
            )
        elif op == "stages" and len(parts) == 3:
            opportunity_id = UUID(parts[2])
            stages = [
                row
                for row in pipeline.list_stages(conn)
                if row["stage_code"] not in {"WON", "LOST"}
            ]
            rows = [
                [button(row["display_name"], f"p3:ss:{opportunity_id}:{row['sort_order']}")]
                for row in stages
            ]
            rows.append([button("Back", f"p3:op:{opportunity_id}")])
            _edit(update, actions, "Choose the opportunity stage:", InlineKeyboardMarkup(rows))
        elif op == "ss" and len(parts) == 4:
            stage = conn.execute(
                "select stage_code from sales_pipeline_stages where sort_order=%s and enabled",
                (int(parts[3]),),
            ).fetchone()
            if not stage:
                raise ValueError("Stage is no longer available")
            pipeline.update_stage(conn, UUID(parts[2]), stage["stage_code"], user_id, update_id)
            _opportunity_card(conn, update, actions, UUID(parts[2]), timezone)
        elif op == "timeline" and len(parts) == 3:
            opportunity_id = UUID(parts[2])
            timeline = pipeline.opportunity_timeline(conn, opportunity_id, 15)
            lines = ["<b>Opportunity Timeline</b>"]
            for item in timeline:
                details = item.get("details") or {}
                summary = (
                    details.get("note") or details.get("reason") or details.get("action") or ""
                )
                lines.append(
                    f"• {html.escape(local_label(item['created_at'], timezone))} · {html.escape(item['event_type'].replace('_', ' ').title())} {html.escape(str(summary)[:180])}"
                )
            if not timeline:
                lines.append("No timeline events have been recorded.")
            _edit(
                update,
                actions,
                "\n".join(lines),
                InlineKeyboardMarkup([[button("Opportunity", f"p3:op:{opportunity_id}")]]),
            )
        elif op == "note" and len(parts) == 3:
            opportunity_id = UUID(parts[2])
            row = pipeline.get_opportunity(conn, opportunity_id)
            if not row:
                raise ValueError("Opportunity was not found")
            _set_state(
                conn,
                user_id,
                "P3_NOTE",
                contact_id=row["contact_id"],
                payload={"opportunity_id": str(opportunity_id)},
            )
            _edit(
                update,
                actions,
                "Send a note for this opportunity.",
                InlineKeyboardMarkup([[button("Cancel", "p3:cancel")]]),
            )
        elif op == "follow" and len(parts) == 3:
            _start_followup(conn, update, user_id, actions, UUID(parts[2]))
        elif op == "ft" and len(parts) == 4:
            opportunity_id, followup_index = UUID(parts[2]), int(parts[3])
            if not 0 <= followup_index < len(FOLLOWUP_TYPES):
                raise ValueError("Unknown follow-up type")
            followup_type = FOLLOWUP_TYPES[followup_index]
            if followup_type == "CUSTOM":
                _set_state(
                    conn,
                    user_id,
                    "P3_FOLLOWUP_CUSTOM_TYPE",
                    payload={"opportunity_id": str(opportunity_id)},
                )
                _edit(
                    update,
                    actions,
                    "Send a short custom follow-up label (up to 40 characters).",
                    InlineKeyboardMarkup([[button("Cancel", "p3:cancel")]]),
                )
                return True
            _set_state(
                conn,
                user_id,
                "P3_FOLLOWUP_TIME",
                payload={"opportunity_id": str(opportunity_id), "followup_type": followup_type},
            )
            _edit(
                update,
                actions,
                f"Choose when to {followup_type.replace('_', ' ').lower()}. Add an optional note after selecting a time.",
                _followup_time_markup(),
            )
        elif op == "ftime" and len(parts) == 3:
            state = _state(conn, user_id)
            if not state or state["purpose"] != "P3_FOLLOWUP_TIME":
                raise ValueError("Follow-up form expired")
            payload = state["payload"] or {}
            if parts[2] == "custom":
                _set_state(conn, user_id, "P3_FOLLOWUP_CUSTOM", payload=payload)
                _edit(
                    update,
                    actions,
                    f"Enter YYYY-MM-DD HH:MM in {timezone}.",
                    InlineKeyboardMarkup([[button("Cancel", "p3:cancel")]]),
                )
            else:
                due = _quick_time(parts[2], timezone)
                _set_state(
                    conn,
                    user_id,
                    "P3_FOLLOWUP_NOTE",
                    payload={**payload, "scheduled_at": due.isoformat()},
                )
                _edit(
                    update,
                    actions,
                    "Send an optional reason or note, or send `-` to leave it blank.",
                    InlineKeyboardMarkup(
                        [[button("Skip Note", "p3:fskip")], [button("Cancel", "p3:cancel")]]
                    ),
                )
        elif op == "fskip":
            state = _state(conn, user_id)
            if not state or state["purpose"] != "P3_FOLLOWUP_NOTE":
                raise ValueError("Follow-up form expired")
            payload = state["payload"] or {}
            followup = pipeline.schedule_opportunity_followup(
                conn,
                UUID(payload["opportunity_id"]),
                datetime.fromisoformat(payload["scheduled_at"]),
                payload["followup_type"],
                user_id,
            )
            _clear_state(conn, user_id)
            _edit(
                update,
                actions,
                f"Follow-up saved for {html.escape(local_label(followup['scheduled_at'], timezone))}.",
                InlineKeyboardMarkup(
                    [
                        [button("Opportunity", f"p3:op:{payload['opportunity_id']}")],
                        [button("Main Menu", "m:home")],
                    ]
                ),
            )
        elif op == "matches" and len(parts) == 3:
            _opportunity_matches(conn, update, actions, UUID(parts[2]))
        elif op == "pickprop" and len(parts) == 4:
            state = _state(conn, user_id)
            if not state or state["purpose"] != "P3_PICK_VISIT_PROPERTY":
                raise ValueError("Property selection expired")
            payload = state["payload"] or {}
            if (
                parts[2] not in payload.get("property_ids", [])
                or payload.get("select_token") != parts[3]
            ):
                raise ValueError("Property selection expired")
            opportunity_id = UUID(payload["opportunity_id"])
            _set_property_link(conn, opportunity_id, UUID(parts[2]), user_id, update_id)
            _clear_state(conn, user_id)
            _opportunity_card(conn, update, actions, opportunity_id, timezone)
        elif op == "vavail" and len(parts) == 3:
            opportunity_id = UUID(parts[2])
            opportunity = pipeline.get_opportunity(conn, opportunity_id)
            if not opportunity or not opportunity.get("primary_property_id"):
                raise ValueError("Select a property before scheduling a visit")
            _set_state(
                conn,
                user_id,
                "P3_VISIT_TIME",
                contact_id=opportunity["contact_id"],
                payload={
                    "opportunity_id": str(opportunity_id),
                    "property_id": str(opportunity["primary_property_id"]),
                    "availability_confirmed": True,
                    "confirm_token": _token(),
                },
            )
            _edit(
                update,
                actions,
                f"Enter the visit time as YYYY-MM-DD HH:MM in {timezone}.",
                InlineKeyboardMarkup([[button("Cancel", "p3:cancel")]]),
            )
        elif op == "visitnew" and len(parts) == 3:
            opportunity_id = UUID(parts[2])
            opportunity = pipeline.get_opportunity(conn, opportunity_id)
            if not opportunity:
                raise ValueError("Opportunity was not found")
            if not opportunity.get("primary_property_id"):
                _opportunity_matches(conn, update, actions, opportunity_id)
            else:
                prop = conn.execute(
                    "select availability_status from properties where id=%s",
                    (opportunity["primary_property_id"],),
                ).fetchone()
                if not prop:
                    raise ValueError("The selected property is unavailable")
                if prop["availability_status"] != "AVAILABLE":
                    _edit(
                        update,
                        actions,
                        "This property is not recorded as available. Confirm that you checked with the seller before scheduling a visit.",
                        InlineKeyboardMarkup(
                            [
                                [
                                    button(
                                        "I Confirmed With the Seller", f"p3:vavail:{opportunity_id}"
                                    )
                                ],
                                [button("Cancel", f"p3:op:{opportunity_id}")],
                            ]
                        ),
                    )
                else:
                    _set_state(
                        conn,
                        user_id,
                        "P3_VISIT_TIME",
                        contact_id=opportunity["contact_id"],
                        payload={
                            "opportunity_id": str(opportunity_id),
                            "property_id": str(opportunity["primary_property_id"]),
                            "availability_confirmed": False,
                            "confirm_token": _token(),
                        },
                    )
                    _edit(
                        update,
                        actions,
                        f"Enter visit time as YYYY-MM-DD HH:MM in {timezone}.",
                        InlineKeyboardMarkup([[button("Cancel", "p3:cancel")]]),
                    )
        elif op == "visits":
            _visit_page(conn, update, actions, timezone)
        elif op == "v" and len(parts) == 3:
            _visit_card(conn, update, actions, UUID(parts[2]), timezone)
        elif op == "vtimeline" and len(parts) == 3:
            visit_id = UUID(parts[2])
            events = pipeline.visit_timeline(conn, visit_id, 20)
            lines = ["<b>Visit Timeline</b>"]
            for event in events:
                lines.append(
                    f"• {html.escape(local_label(event['created_at'], timezone))} · "
                    f"{html.escape(event['event_type'].replace('_', ' ').title())}"
                )
            if not events:
                lines.append("No visit events are recorded yet.")
            _edit(
                update,
                actions,
                "\n".join(lines),
                InlineKeyboardMarkup([[button("Back to Visit", f"p3:v:{visit_id}")]]),
            )
        elif op == "vskip":
            state = _state(conn, user_id)
            if not state or state["purpose"] != "P3_VISIT_DETAILS":
                raise ValueError("Visit details form expired")
            payload = state.get("payload") or {}
            payload["details"] = {}
            _set_state(
                conn, user_id, "P3_VISIT_SAVE", contact_id=state.get("contact_id"), payload=payload
            )
            _visit_save_prompt(update, actions, payload)
        elif op == "vsave" and len(parts) == 3:
            _create_visit_from_state(
                conn, update, user_id, update_id, actions, timezone, confirm_token=parts[2]
            )
        elif op == "vdup" and len(parts) == 3:
            _create_visit_from_state(
                conn,
                update,
                user_id,
                update_id,
                actions,
                timezone,
                duplicate_confirmed=True,
                confirm_token=parts[2],
            )
        elif op in {"vc", "va", "vd", "vn", "vx", "vrr"} and len(parts) == 3:
            action = {
                "vc": "CONFIRM",
                "va": "ARRIVED",
                "vd": "COMPLETE",
                "vn": "NO_SHOW",
                "vx": "CANCEL",
                "vrr": "RESCHEDULE_REQUESTED",
            }[op]
            visit_id = UUID(parts[2])
            if action in {"COMPLETE", "NO_SHOW", "CANCEL"}:
                confirm_token = _token()
                _set_state(
                    conn,
                    user_id,
                    "P3_VISIT_CONFIRM",
                    payload={
                        "visit_id": str(visit_id),
                        "action": action,
                        "confirm_token": confirm_token,
                    },
                )
                _edit(
                    update,
                    actions,
                    f"Confirm {action.replace('_', ' ').lower()} for this visit?",
                    InlineKeyboardMarkup(
                        [
                            [button("Confirm", f"p3:vyes:{visit_id}:{action}:{confirm_token}")],
                            [button("Cancel", f"p3:v:{visit_id}")],
                        ]
                    ),
                )
            else:
                pipeline.update_visit(conn, visit_id, action, user_id, update_id, confirmed=True)
                _visit_card(conn, update, actions, visit_id, timezone)
        elif op == "vyes" and len(parts) == 5:
            visit_id, action = UUID(parts[2]), parts[3]
            state = _state(conn, user_id)
            payload = state.get("payload") or {} if state else {}
            if (
                not state
                or state["purpose"] != "P3_VISIT_CONFIRM"
                or payload.get("visit_id") != str(visit_id)
                or payload.get("action") != action
                or payload.get("confirm_token") != parts[4]
            ):
                raise ValueError("Visit confirmation expired")
            pipeline.update_visit(conn, visit_id, action, user_id, update_id, confirmed=True)
            _clear_state(conn, user_id)
            _visit_card(conn, update, actions, visit_id, timezone)
        elif op == "vr" and len(parts) == 3:
            visit_id = UUID(parts[2])
            old_visit = pipeline.get_visit(conn, visit_id)
            if not old_visit:
                raise ValueError("Visit not found")
            if old_visit["visit_status"] not in {"SCHEDULED", "BUYER_ARRIVED"}:
                raise ValueError("This visit is already resolved")
            if old_visit["property_availability"] != "AVAILABLE":
                _edit(
                    update,
                    actions,
                    "This property is not recorded as available. Confirm that you checked with the seller before rescheduling.",
                    InlineKeyboardMarkup(
                        [
                            [button("I Confirmed With the Seller", f"p3:vrseller:{visit_id}")],
                            [button("Back", f"p3:v:{visit_id}")],
                        ]
                    ),
                )
                return True
            _set_state(
                conn,
                user_id,
                "P3_VISIT_RESCHEDULE",
                contact_id=old_visit["contact_id"],
                payload={
                    "visit_id": str(visit_id),
                    "opportunity_id": str(old_visit["opportunity_id"]),
                    "property_id": str(old_visit["property_id"]),
                    "availability_confirmed": False,
                    "confirm_token": _token(),
                },
            )
            _edit(
                update,
                actions,
                f"Enter the new visit time as YYYY-MM-DD HH:MM in {timezone}.",
                InlineKeyboardMarkup([[button("Cancel", f"p3:v:{visit_id}")]]),
            )
        elif op == "vrseller" and len(parts) == 3:
            visit_id = UUID(parts[2])
            old_visit = pipeline.get_visit(conn, visit_id)
            if not old_visit or old_visit["visit_status"] not in {"SCHEDULED", "BUYER_ARRIVED"}:
                raise ValueError("This visit cannot be rescheduled")
            _set_state(
                conn,
                user_id,
                "P3_VISIT_RESCHEDULE",
                contact_id=old_visit["contact_id"],
                payload={
                    "visit_id": str(visit_id),
                    "opportunity_id": str(old_visit["opportunity_id"]),
                    "property_id": str(old_visit["property_id"]),
                    "availability_confirmed": True,
                    "confirm_token": _token(),
                },
            )
            _edit(
                update,
                actions,
                f"Enter the new visit time as YYYY-MM-DD HH:MM in {timezone}.",
                InlineKeyboardMarkup([[button("Cancel", f"p3:v:{visit_id}")]]),
            )
        elif op == "vf" and len(parts) == 3:
            visit_id = UUID(parts[2])
            rows = []
            for i in range(0, len(VISIT_OUTCOMES), 2):
                rows.append(
                    [
                        button(
                            code.replace("_", " ").title(),
                            f"p3:vfo:{visit_id}:{VISIT_OUTCOMES.index(code)}",
                        )
                        for code in VISIT_OUTCOMES[i : i + 2]
                    ]
                )
            rows.append([button("Cancel", f"p3:v:{visit_id}")])
            _edit(update, actions, "Choose the visit outcome:", InlineKeyboardMarkup(rows))
        elif op == "vfo" and len(parts) == 4:
            visit_id, outcome_index = UUID(parts[2]), int(parts[3])
            if not 0 <= outcome_index < len(VISIT_OUTCOMES):
                raise ValueError("Choose a supported outcome")
            outcome = VISIT_OUTCOMES[outcome_index]
            _set_state(
                conn,
                user_id,
                "P3_VISIT_FEEDBACK",
                payload={"visit_id": str(visit_id), "outcome": outcome},
            )
            _edit(
                update,
                actions,
                "Add what the buyer liked, their concerns, offer interest, alternatives needed, and the next action. Send one concise note.",
                InlineKeyboardMarkup(
                    [[button("Skip Notes", "p3:vfskip")], [button("Cancel", f"p3:v:{visit_id}")]]
                ),
            )
        elif op == "vfskip":
            state = _state(conn, user_id)
            if not state or state["purpose"] != "P3_VISIT_FEEDBACK":
                raise ValueError("Visit feedback form expired")
            payload = state["payload"] or {}
            outcome = payload["outcome"]
            pipeline.update_visit(
                conn,
                UUID(payload["visit_id"]),
                "FEEDBACK",
                user_id,
                update_id,
                feedback={"outcome": outcome},
                buyer_liked_property=outcome
                in {"STRONG_INTEREST", "INTERESTED_WITH_CONCERNS", "PRICE_NEGOTIATION"},
            )
            _clear_state(conn, user_id)
            _visit_card(conn, update, actions, UUID(payload["visit_id"]), timezone)
        elif op == "fd" and len(parts) == 3:
            row = conn.execute("select * from follow_ups where id=%s", (UUID(parts[2]),)).fetchone()
            if not row:
                raise ValueError("Follow-up not found")
            rows = [
                [button("Complete", f"p3:fdone:{row['id']}")],
                [button("Reschedule", f"p3:fres:{row['id']}")],
                [button("Cancel Follow-Up", f"p3:fcancel:{row['id']}")],
                [button("Back", "p3:followups")],
            ]
            _edit(
                update,
                actions,
                f"<b>{row['follow_up_type'].replace('_', ' ').title()}</b> · {html.escape(local_label(row['scheduled_at'], timezone))}\n{html.escape(row.get('reason') or row.get('notes') or '')}",
                InlineKeyboardMarkup(rows),
            )
        elif op == "fdone" and len(parts) == 3:
            row = conn.execute(
                "update follow_ups set status='COMPLETED',completed_at=now(),completed_by=%s,completion_outcome='COMPLETED_BY_AGENT',updated_at=now() where id=%s and status='PENDING' returning contact_id,opportunity_id",
                (user_id, UUID(parts[2])),
            ).fetchone()
            if not row:
                raise ValueError("This follow-up was already completed")
            conn.execute(
                """update contacts set next_follow_up_at=(select min(scheduled_at) from follow_ups where contact_id=%s and status='PENDING'),follow_up_state=case when exists(select 1 from follow_ups where contact_id=%s and status='PENDING' and scheduled_at<now()) then 'OVERDUE' when exists(select 1 from follow_ups where contact_id=%s and status='PENDING') then 'PENDING' else 'NONE' end,updated_at=now() where id=%s""",
                (row["contact_id"], row["contact_id"], row["contact_id"], row["contact_id"]),
            )
            pipeline._record_audit(
                conn,
                actor_id=user_id,
                event_type="FOLLOW_UP_COMPLETED",
                entity_type="FOLLOW_UP",
                entity_id=UUID(parts[2]),
                contact_id=row["contact_id"],
                new_value={"completion_outcome": "COMPLETED_BY_AGENT"},
            )
            if row.get("opportunity_id"):
                conn.execute(
                    "update sales_opportunities set next_action=null,next_action_due=null,last_interaction_at=now(),updated_at=now() where id=%s and next_action_due<=now()",
                    (row["opportunity_id"],),
                )
            _edit(
                update,
                actions,
                "Follow-up completed.",
                InlineKeyboardMarkup(
                    [
                        [button("Today's Follow-Ups", "p3:followups")],
                        [button("Main Menu", "m:home")],
                    ]
                ),
            )
        elif op == "fres" and len(parts) == 3:
            follow_id = UUID(parts[2])
            old = conn.execute(
                "select * from follow_ups where id=%s and status='PENDING'", (follow_id,)
            ).fetchone()
            if not old:
                raise ValueError("Follow-up is no longer pending")
            _set_state(
                conn,
                user_id,
                "P3_FOLLOWUP_RESCHEDULE",
                contact_id=old["contact_id"],
                payload={
                    "follow_up_id": str(follow_id),
                    "opportunity_id": str(old["opportunity_id"])
                    if old.get("opportunity_id")
                    else None,
                    "follow_up_type": old["follow_up_type"],
                    "reason": old.get("reason"),
                    "notes": old.get("notes"),
                },
            )
            _edit(
                update,
                actions,
                f"Enter a new due time as YYYY-MM-DD HH:MM in {timezone}.",
                InlineKeyboardMarkup([[button("Cancel", f"p3:fd:{follow_id}")]]),
            )
        elif op == "fcancel" and len(parts) == 3:
            follow_id = UUID(parts[2])
            old = conn.execute(
                "select contact_id from follow_ups where id=%s and status='PENDING'",
                (follow_id,),
            ).fetchone()
            if not old:
                raise ValueError("Follow-up is no longer pending")
            _set_state(
                conn,
                user_id,
                "P3_FOLLOWUP_CANCEL",
                contact_id=old["contact_id"],
                payload={"follow_up_id": str(follow_id)},
            )
            _edit(
                update,
                actions,
                "Cancel this scheduled follow-up?",
                InlineKeyboardMarkup(
                    [
                        [button("Confirm Cancellation", f"p3:fcyes:{follow_id}")],
                        [button("Back", f"p3:fd:{follow_id}")],
                    ]
                ),
            )
        elif op == "fcyes" and len(parts) == 3:
            follow_id = UUID(parts[2])
            state = _state(conn, user_id)
            payload = state.get("payload") or {} if state else {}
            if (
                not state
                or state["purpose"] != "P3_FOLLOWUP_CANCEL"
                or payload.get("follow_up_id") != str(follow_id)
            ):
                raise ValueError("Follow-up cancellation expired")
            old = conn.execute(
                "update follow_ups set status='CANCELLED',updated_at=now() where id=%s and status='PENDING' returning contact_id",
                (follow_id,),
            ).fetchone()
            if not old:
                raise ValueError("Follow-up is no longer pending")
            conn.execute(
                """update contacts set next_follow_up_at=(select min(scheduled_at) from follow_ups
                     where contact_id=%s and status='PENDING'),
                     follow_up_state=case when exists(select 1 from follow_ups where contact_id=%s
                       and status='PENDING' and scheduled_at<now()) then 'OVERDUE'
                       when exists(select 1 from follow_ups where contact_id=%s and status='PENDING')
                       then 'PENDING' else 'NONE' end,updated_at=now() where id=%s""",
                (old["contact_id"], old["contact_id"], old["contact_id"], old["contact_id"]),
            )
            pipeline._record_audit(
                conn,
                actor_id=user_id,
                event_type="FOLLOW_UP_CANCELLED",
                entity_type="FOLLOW_UP",
                entity_id=follow_id,
                contact_id=old["contact_id"],
                request_key=f"{user_id}:{update_id}",
            )
            _clear_state(conn, user_id)
            _edit(
                update,
                actions,
                "Follow-up cancelled.",
                InlineKeyboardMarkup([[button("Today's Follow-Ups", "p3:followups")]]),
            )
        elif op == "neg" and len(parts) == 3:
            opportunity_id = UUID(parts[2])
            rows = [
                [button("Record Buyer Offer", f"p3:nt:{opportunity_id}:buyer")],
                [button("Record Seller Counteroffer", f"p3:nt:{opportunity_id}:seller")],
                [button("Payment Plan Discussion", f"p3:nt:{opportunity_id}:payment")],
                [button("Negotiation Timeline", f"p3:negtimeline:{opportunity_id}")],
                [button("Change Status", f"p3:nstatus:{opportunity_id}")],
                [button("Back", f"p3:op:{opportunity_id}")],
            ]
            _edit(
                update,
                actions,
                "<b>Negotiation</b>\nOffers are recorded proposals, not legal agreements.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "nt" and len(parts) == 4:
            opportunity_id, kind = UUID(parts[2]), parts[3]
            if kind not in {"buyer", "seller", "payment"}:
                raise ValueError("Unknown negotiation action")
            _set_state(
                conn,
                user_id,
                "P3_NEGOTIATION",
                payload={"opportunity_id": str(opportunity_id), "kind": kind},
            )
            prompt = (
                "Send amount and currency, e.g. 24,500,000 ETB. Optional: add a note after a vertical bar."
                if kind != "payment"
                else "Send down payment, currency, and payment plan separated by |. Example: 5000000 | ETB | 12 monthly installments"
            )
            _edit(
                update,
                actions,
                prompt,
                InlineKeyboardMarkup([[button("Cancel", f"p3:neg:{opportunity_id}")]]),
            )
        elif op == "nstatus" and len(parts) == 3:
            opportunity_id = UUID(parts[2])
            rows = [
                [button(status.replace("_", " ").title(), f"p3:ns:{opportunity_id}:{index}")]
                for index, status in enumerate(NEGOTIATION_STAGE_OPTIONS)
            ]
            rows.append([button("Back", f"p3:neg:{opportunity_id}")])
            _edit(
                update, actions, "Choose a recorded negotiation status:", InlineKeyboardMarkup(rows)
            )
        elif op == "ns" and len(parts) == 4:
            opportunity_id, status_index = UUID(parts[2]), int(parts[3])
            if not 0 <= status_index < len(NEGOTIATION_STAGE_OPTIONS):
                raise ValueError("Choose a supported negotiation status")
            status = NEGOTIATION_STAGE_OPTIONS[status_index]
            if status == "ACCEPTED_RECORDED":
                _set_state(
                    conn,
                    user_id,
                    "P3_NEGOTIATION_STATUS_CONFIRM",
                    payload={"opportunity_id": str(opportunity_id), "status": status},
                )
                _edit(
                    update,
                    actions,
                    "Confirm that acceptance was recorded by an authorized source?",
                    InlineKeyboardMarkup(
                        [
                            [button("Confirm Recorded Acceptance", f"p3:nsyes:{opportunity_id}")],
                            [button("Cancel", f"p3:neg:{opportunity_id}")],
                        ]
                    ),
                )
            else:
                pipeline.save_negotiation(
                    conn,
                    opportunity_id,
                    NegotiationInput(negotiation_status=status, event_type="STATUS_CHANGED"),
                    user_id,
                    update_id,
                )
                _edit(
                    update,
                    actions,
                    "Negotiation status recorded.",
                    InlineKeyboardMarkup([[button("Opportunity", f"p3:op:{opportunity_id}")]]),
                )
        elif op == "nsyes" and len(parts) == 3:
            opportunity_id = UUID(parts[2])
            state = _state(conn, user_id)
            payload = state.get("payload") or {} if state else {}
            if (
                not state
                or state["purpose"] != "P3_NEGOTIATION_STATUS_CONFIRM"
                or payload.get("opportunity_id") != str(opportunity_id)
                or payload.get("status") != "ACCEPTED_RECORDED"
            ):
                raise ValueError("Negotiation confirmation expired")
            pipeline.save_negotiation(
                conn,
                opportunity_id,
                NegotiationInput(
                    negotiation_status="ACCEPTED_RECORDED", event_type="STATUS_CHANGED"
                ),
                user_id,
                update_id,
            )
            _clear_state(conn, user_id)
            _edit(
                update,
                actions,
                "Negotiation status recorded.",
                InlineKeyboardMarkup([[button("Opportunity", f"p3:op:{opportunity_id}")]]),
            )
        elif op == "negtimeline" and len(parts) == 3:
            opportunity_id = UUID(parts[2])
            record = conn.execute(
                "select id from negotiation_records where opportunity_id=%s", (opportunity_id,)
            ).fetchone()
            if not record:
                _edit(
                    update,
                    actions,
                    "No negotiation events are recorded yet.",
                    InlineKeyboardMarkup([[button("Back", f"p3:neg:{opportunity_id}")]]),
                )
            else:
                events = pipeline.negotiation_timeline(conn, record["id"], 20)
                lines = ["<b>Negotiation Timeline</b>"]
                for event in events:
                    detail = event.get("details") or {}
                    values = detail.get("recorded_fields", {})
                    lines.append(
                        f"• {html.escape(local_label(event['created_at'], timezone))} · {html.escape(event['event_type'].replace('_', ' ').title())} · {html.escape(json.dumps(values, ensure_ascii=False)[:160])}"
                    )
                _edit(
                    update,
                    actions,
                    "\n".join(lines),
                    InlineKeyboardMarkup([[button("Negotiation", f"p3:neg:{opportunity_id}")]]),
                )
        elif op == "reserve" and len(parts) == 3:
            opportunity_id = UUID(parts[2])
            opportunity = pipeline.get_opportunity(conn, opportunity_id)
            if not opportunity:
                raise ValueError("Opportunity not found")
            if not opportunity.get("property_id") and not opportunity.get("primary_property_id"):
                _opportunity_matches(conn, update, actions, opportunity_id)
            else:
                _set_state(
                    conn,
                    user_id,
                    "P3_RESERVATION",
                    payload={
                        "opportunity_id": str(opportunity_id),
                        "property_id": str(opportunity.get("primary_property_id")),
                    },
                )
                _edit(
                    update,
                    actions,
                    "Send amount | currency | reservation date (YYYY-MM-DD). Example: 100000 | ETB | 2026-10-20. This will be recorded as requested, not confirmed.",
                    InlineKeyboardMarkup([[button("Cancel", f"p3:op:{opportunity_id}")]]),
                )
        elif op == "resv" and len(parts) == 3:
            _reservation_card(conn, update, actions, UUID(parts[2]))
        elif op == "rconfirm" and len(parts) == 3:
            state = _state(conn, user_id)
            if not state or state["purpose"] != "P3_RESERVATION_CONFIRM":
                raise ValueError("Reservation confirmation expired")
            payload = state["payload"] or {}
            if payload.get("confirm_token") != parts[2]:
                raise ValueError("Reservation confirmation expired")
            reservation = pipeline.create_reservation(
                conn,
                UUID(payload["opportunity_id"]),
                UUID(payload["property_id"]),
                payload["values"],
                user_id,
                update_id,
                confirmed=True,
            )
            _clear_state(conn, user_id)
            _edit(
                update,
                actions,
                f"Reservation request recorded for {reservation['currency'].strip()} {reservation['reservation_amount']:,.2f}. Confirmation and payment remain unrecorded.",
                InlineKeyboardMarkup(
                    [
                        [button("View Reservation", f"p3:resv:{reservation['id']}")],
                        [button("Opportunity", f"p3:op:{payload['opportunity_id']}")],
                    ]
                ),
            )
        elif op == "rc" and len(parts) == 3:
            reservation_id = UUID(parts[2])
            row = conn.execute(
                "select reservation_status from reservations where id=%s", (reservation_id,)
            ).fetchone()
            if not row:
                raise ValueError("Reservation not found")
            _set_state(
                conn,
                user_id,
                "P3_RESERVATION_STATUS",
                payload={"reservation_id": str(reservation_id), "status": "CONFIRMED"},
            )
            _edit(
                update,
                actions,
                "Confirm that the reservation was confirmed by an authorized source?",
                InlineKeyboardMarkup(
                    [
                        [button("Confirm Recorded Status", f"p3:ryes:{reservation_id}:CONFIRMED")],
                        [button("Cancel", f"p3:resv:{reservation_id}")],
                    ]
                ),
            )
        elif op == "rp" and len(parts) == 4:
            reservation_id, payment_status = UUID(parts[2]), parts[3]
            if payment_status not in pipeline.PAYMENT_STATUSES:
                raise ValueError("Invalid payment status")
            _set_state(
                conn,
                user_id,
                "P3_RESERVATION_PAYMENT",
                payload={"reservation_id": str(reservation_id), "payment_status": payment_status},
            )
            _edit(
                update,
                actions,
                f"Confirm the recorded payment status as {payment_status.replace('_', ' ').title()}?",
                InlineKeyboardMarkup(
                    [
                        [
                            button(
                                "Confirm Status", f"p3:ryes:{reservation_id}:PAY:{payment_status}"
                            )
                        ],
                        [button("Cancel", f"p3:resv:{reservation_id}")],
                    ]
                ),
            )
        elif op == "rx" and len(parts) == 3:
            reservation_id = UUID(parts[2])
            _set_state(
                conn,
                user_id,
                "P3_RESERVATION_STATUS",
                payload={"reservation_id": str(reservation_id), "status": "CANCELLED"},
            )
            _edit(
                update,
                actions,
                "Confirm reservation cancellation?",
                InlineKeyboardMarkup(
                    [
                        [button("Confirm Cancellation", f"p3:ryes:{reservation_id}:CANCELLED")],
                        [button("Back", f"p3:resv:{reservation_id}")],
                    ]
                ),
            )
        elif op == "ryes" and len(parts) in {4, 5}:
            reservation_id = UUID(parts[2])
            state = _state(conn, user_id)
            payload = state.get("payload") or {} if state else {}
            if len(parts) == 5 and parts[3] == "PAY":
                if (
                    not state
                    or state["purpose"] != "P3_RESERVATION_PAYMENT"
                    or payload.get("reservation_id") != str(reservation_id)
                    or payload.get("payment_status") != parts[4]
                ):
                    raise ValueError("Payment confirmation expired")
                pipeline.update_reservation(
                    conn,
                    reservation_id,
                    payment_status=parts[4],
                    actor_id=user_id,
                    confirmed=True,
                    update_id=update_id,
                )
            else:
                if (
                    not state
                    or state["purpose"] != "P3_RESERVATION_STATUS"
                    or payload.get("reservation_id") != str(reservation_id)
                    or payload.get("status") != parts[3]
                ):
                    raise ValueError("Reservation confirmation expired")
                pipeline.update_reservation(
                    conn,
                    reservation_id,
                    status=parts[3],
                    actor_id=user_id,
                    confirmed=True,
                    update_id=update_id,
                )
            _clear_state(conn, user_id)
            _reservation_card(conn, update, actions, reservation_id)
        elif op == "sale" and len(parts) == 3:
            opportunity_id = UUID(parts[2])
            opportunity = pipeline.get_opportunity(conn, opportunity_id)
            if not opportunity:
                raise ValueError("Opportunity not found")
            if not opportunity.get("primary_property_id"):
                _opportunity_matches(conn, update, actions, opportunity_id)
            else:
                _set_state(
                    conn, user_id, "P3_SALE", payload={"opportunity_id": str(opportunity_id)}
                )
                _edit(
                    update,
                    actions,
                    "Enter final recorded price | currency | sale date (YYYY-MM-DD). It will be pending verification until an authorized source is checked.",
                    InlineKeyboardMarkup([[button("Cancel", f"p3:op:{opportunity_id}")]]),
                )
        elif op == "sconfirm" and len(parts) == 3:
            state = _state(conn, user_id)
            if not state or state["purpose"] != "P3_SALE_CONFIRM":
                raise ValueError("Sale confirmation expired")
            payload = state["payload"] or {}
            if payload.get("confirm_token") != parts[2]:
                raise ValueError("Sale confirmation expired")
            opportunity_id = UUID(payload["opportunity_id"])
            row = pipeline.record_sale(
                conn, opportunity_id, payload["sale"], user_id, update_id, confirmed=True
            )
            _clear_state(conn, user_id)
            _edit(
                update,
                actions,
                f"Sale recorded for <b>{row['recorded_sale_price']:,.2f} {row['currency'].strip()}</b> · <b>Pending verification</b>. No payment or legal contract is implied.",
                InlineKeyboardMarkup(
                    [
                        [button("View Opportunity", f"p3:op:{opportunity_id}")],
                        [button("Command Center", "p3:command")],
                    ]
                ),
            )
        elif op == "verify" and len(parts) == 3:
            _sale_status_prompt(conn, update, user_id, actions, UUID(parts[2]), "VERIFIED")
        elif op == "salestatus" and len(parts) == 4:
            _sale_status_prompt(conn, update, user_id, actions, UUID(parts[2]), parts[3])
        elif op == "saleview" and len(parts) == 3:
            _sale_card(conn, update, actions, UUID(parts[2]))
        elif op == "syes" and len(parts) == 5:
            state = _state(conn, user_id)
            payload = state.get("payload") or {} if state else {}
            if (
                not state
                or state["purpose"] != "P3_SALE_STATUS"
                or payload.get("sale_id") != parts[2]
                or payload.get("status") != parts[3]
                or payload.get("confirm_token") != parts[4]
            ):
                raise ValueError("Sale verification confirmation expired")
            row = pipeline.set_sale_verification(
                conn, UUID(parts[2]), parts[3], user_id, confirmed=True
            )
            _clear_state(conn, user_id)
            _sale_card(conn, update, actions, row["id"])
        elif op == "lost" and len(parts) == 3:
            _lost_reasons(update, actions, UUID(parts[2]))
        elif op == "lr" and len(parts) == 4:
            opportunity_id, reason_index = UUID(parts[2]), int(parts[3])
            reasons = list(LOST_LABELS)
            if not 0 <= reason_index < len(reasons):
                raise ValueError("Unknown lost reason")
            reason = reasons[reason_index]
            _set_state(
                conn,
                user_id,
                "P3_LOST_EXPLANATION",
                payload={"opportunity_id": str(opportunity_id), "reason": reason},
            )
            _edit(
                update,
                actions,
                "Send an optional explanation. Send `-` if you do not know or do not want to add one.",
                InlineKeyboardMarkup(
                    [
                        [button("Skip Explanation", "p3:lskip")],
                        [button("Cancel", f"p3:op:{opportunity_id}")],
                    ]
                ),
            )
        elif op == "lskip":
            state = _state(conn, user_id)
            if not state or state["purpose"] != "P3_LOST_EXPLANATION":
                raise ValueError("Close form expired")
            payload = state["payload"] or {}
            payload["explanation"] = None
            payload["confirm_token"] = _token()
            _set_state(conn, user_id, "P3_LOST_CONFIRM", payload=payload)
            _edit(
                update,
                actions,
                f"Close as lost · {LOST_LABELS[payload['reason']]}? Choose whether this opportunity may be considered for future follow-up. Do Not Contact restrictions remain in force.",
                InlineKeyboardMarkup(
                    [
                        [
                            button(
                                "Keep Eligible for Future Follow-Up",
                                f"p3:lyes:{payload['opportunity_id']}:1:{payload['confirm_token']}",
                            )
                        ],
                        [
                            button(
                                "No Future Follow-Up",
                                f"p3:lyes:{payload['opportunity_id']}:0:{payload['confirm_token']}",
                            )
                        ],
                        [button("Cancel", f"p3:op:{payload['opportunity_id']}")],
                    ]
                ),
            )
        elif op == "lyes" and len(parts) == 5:
            state = _state(conn, user_id)
            if not state or state["purpose"] != "P3_LOST_CONFIRM":
                raise ValueError("Lost confirmation expired")
            payload = state["payload"] or {}
            if (
                payload.get("opportunity_id") != parts[2]
                or payload.get("confirm_token") != parts[4]
                or parts[3] not in {"0", "1"}
            ):
                raise ValueError("Lost confirmation expired")
            pipeline.close_lost(
                conn,
                UUID(payload["opportunity_id"]),
                payload["reason"],
                payload.get("explanation"),
                user_id,
                update_id,
                confirmed=True,
                keep_future_followup=parts[3] == "1",
            )
            _clear_state(conn, user_id)
            _edit(
                update,
                actions,
                "Opportunity closed as lost. The contact and other opportunities remain in the CRM.",
                InlineKeyboardMarkup(
                    [[button("Pipeline", "p3:home")], [button("Main Menu", "m:home")]]
                ),
            )
        elif op == "reopen" and len(parts) == 3:
            opportunity_id = UUID(parts[2])
            confirm_token = _token()
            _set_state(
                conn,
                user_id,
                "P3_REOPEN",
                payload={"opportunity_id": str(opportunity_id), "confirm_token": confirm_token},
            )
            _edit(
                update,
                actions,
                "Reopen this lost opportunity as a new opportunity discussion?",
                InlineKeyboardMarkup(
                    [
                        [
                            button(
                                "Confirm Reopen", f"p3:reopenyes:{opportunity_id}:{confirm_token}"
                            )
                        ],
                        [button("Cancel", f"p3:op:{opportunity_id}")],
                    ]
                ),
            )
        elif op == "reopenyes" and len(parts) == 4:
            opportunity_id = UUID(parts[2])
            state = _state(conn, user_id)
            payload = state.get("payload") or {} if state else {}
            if (
                not state
                or state["purpose"] != "P3_REOPEN"
                or payload.get("opportunity_id") != str(opportunity_id)
                or payload.get("confirm_token") != parts[3]
            ):
                raise ValueError("Reopen confirmation expired")
            pipeline.reopen_lost(conn, opportunity_id, user_id, update_id, confirmed=True)
            _clear_state(conn, user_id)
            _opportunity_card(conn, update, actions, opportunity_id, timezone)
        elif op == "react":
            _reactivation_page(conn, update, actions, int(parts[2]) if len(parts) > 2 else 0)
        elif op == "reactitem" and len(parts) == 3:
            _reactivation_card(conn, update, actions, UUID(parts[2]))
        elif op == "reactdismiss" and len(parts) == 3:
            pipeline.act_on_reactivation(conn, UUID(parts[2]), "DISMISS", user_id, confirmed=True)
            _reactivation_page(conn, update, actions, 0)
        elif op == "reactcall" and len(parts) == 3:
            result = pipeline.act_on_reactivation(
                conn, UUID(parts[2]), "CALL_NOW", user_id, confirmed=True
            )
            contact = crm.get_contact(conn, result["contact_id"])
            if not contact or not pipeline.contact_is_callable(contact):
                raise ValueError("Contact is no longer eligible for calling")
            actions.append(
                {
                    "type": "dialer",
                    "name": contact["name"],
                    "tel_uri": f"tel:{contact['normalized_phone']}",
                }
            )
            _edit(
                update,
                actions,
                "Opening the phone dialer. Tap the phone app's call button to place the call.",
                InlineKeyboardMarkup(
                    [
                        [button("Open Contact", f"lead:{contact['id']}")],
                        [button("Queue", "p3:react:0")],
                    ]
                ),
            )
        elif op == "reactschedule" and len(parts) == 3:
            rec = conn.execute(
                """select r.*,c.contact_eligibility,c.lead_status,c.conversion_state,
                          c.normalized_phone from reactivation_recommendations r
                   join contacts c on c.id=r.contact_id where r.id=%s and r.status='OPEN'""",
                (UUID(parts[2]),),
            ).fetchone()
            if not rec:
                raise ValueError("Suggestion is no longer open")
            if not pipeline.contact_is_callable(rec):
                raise ValueError("This contact is not eligible for an outbound follow-up")
            _set_state(
                conn,
                user_id,
                "P3_REACT_FOLLOWUP",
                contact_id=rec["contact_id"],
                payload={
                    "recommendation_id": str(rec["id"]),
                    "opportunity_id": str(rec["opportunity_id"]) if rec["opportunity_id"] else None,
                },
            )
            _edit(
                update,
                actions,
                f"Enter follow-up date and time in {timezone} as YYYY-MM-DD HH:MM.",
                InlineKeyboardMarkup([[button("Cancel", f"p3:reactitem:{rec['id']}")]]),
            )
        elif op == "reactmatch" and len(parts) == 3:
            rec = pipeline.act_on_reactivation(
                conn, UUID(parts[2]), "FIND_ALTERNATIVES", user_id, confirmed=True
            )
            matches = intelligence.find_matching_properties(conn, rec["contact_id"], limit=8)
            text = "<b>Available alternatives</b>"
            rows = [
                [
                    button(
                        f"{m['property']['title'][:28]} · {m['match']['score']} pts",
                        f"p2:bprop:{m['property']['id']}",
                    )
                ]
                for m in matches
            ]
            if not matches:
                text += "\nNo matching available property is recorded for this buyer."
            rows.extend(
                [
                    [button("Open Contact", f"lead:{rec['contact_id']}")],
                    [button("Reactivation Queue", "p3:react:0")],
                ]
            )
            _edit(update, actions, text, InlineKeyboardMarkup(rows))
        elif op == "followups":
            _followup_page(conn, update, actions, timezone)
        elif op == "negs":
            rows = conn.execute(
                """select n.opportunity_id,n.negotiation_status,c.name,n.next_action_due from negotiation_records n join sales_opportunities o on o.id=n.opportunity_id join contacts c on c.id=o.contact_id where n.negotiation_status not in ('REJECTED','EXPIRED','CANCELLED') order by n.next_action_due nulls last,n.updated_at desc limit 20"""
            ).fetchall()
            buttons = [
                [
                    button(
                        f"{row['name'][:24]} · {row['negotiation_status'].replace('_', ' ').title()}",
                        f"p3:neg:{row['opportunity_id']}",
                    )
                ]
                for row in rows
            ]
            buttons.extend([[button("Back", "p3:command")]])
            _edit(update, actions, "<b>Active Negotiations</b>", InlineKeyboardMarkup(buttons))
        elif op == "reservations":
            _reservation_list(conn, update, actions)
        elif op == "commissions":
            _commission_menu(conn, update, actions)
        elif op == "commlist" and len(parts) == 3:
            _commission_list(conn, update, actions, int(parts[2]), user_id)
        elif op == "commission" and len(parts) == 3:
            _commission_card(conn, update, actions, UUID(parts[2]), user_id)
        elif op == "rule" and len(parts) == 3:
            rule_id = UUID(parts[2])
            rule = conn.execute("select * from commission_rules where id=%s", (rule_id,)).fetchone()
            if not rule:
                raise ValueError("Commission rule not found")
            value = (
                f"{rule['amount_or_rate']:,.4f}%"
                if rule["rule_type"] == "PERCENTAGE"
                else f"{rule['currency'].strip()} {rule['amount_or_rate']:,.2f}"
            )
            scope = rule.get("project_name") or "All projects"
            agent = str(rule.get("agent_telegram_user_id") or "All agents")
            _edit(
                update,
                actions,
                f"<b>{html.escape(rule['rule_name'])}</b> · {rule['rule_type'].title()} {html.escape(value)}\nProject: {html.escape(scope)}\nAgent: {html.escape(agent)}\nStatus: {'Active' if rule['active'] else 'Inactive'}\nThis rule applies to sale records created from now on.",
                InlineKeyboardMarkup(
                    [
                        [
                            button(
                                "Deactivate Rule" if rule["active"] else "Activate Rule",
                                f"p3:ruletoggle:{rule_id}",
                            )
                        ],
                        [button("Commission Rules", "p3:commissions")],
                    ]
                ),
            )
        elif op == "ruletoggle" and len(parts) == 3:
            rule_id = UUID(parts[2])
            rule = conn.execute(
                "select active from commission_rules where id=%s", (rule_id,)
            ).fetchone()
            if not rule:
                raise ValueError("Commission rule not found")
            active = not rule["active"]
            _set_state(
                conn,
                user_id,
                "P3_COMMISSION_RULE_STATUS",
                payload={"rule_id": str(rule_id), "active": active},
            )
            _edit(
                update,
                actions,
                f"Confirm {'activating' if active else 'deactivating'} this commission rule? Existing sale records will not be recalculated.",
                InlineKeyboardMarkup(
                    [
                        [button("Confirm Rule Change", f"p3:ruleyes:{rule_id}:{int(active)}")],
                        [button("Back", f"p3:rule:{rule_id}")],
                    ]
                ),
            )
        elif op == "ruleyes" and len(parts) == 4:
            rule_id = UUID(parts[2])
            state = _state(conn, user_id)
            payload = state.get("payload") or {} if state else {}
            if (
                not state
                or state["purpose"] != "P3_COMMISSION_RULE_STATUS"
                or payload.get("rule_id") != str(rule_id)
                or payload.get("active") != (parts[3] == "1")
            ):
                raise ValueError("Commission rule confirmation expired")
            rule = pipeline.update_commission_rule(
                conn,
                rule_id,
                active=bool(payload["active"]),
                actor_id=user_id,
                confirmed=True,
            )
            _clear_state(conn, user_id)
            _edit(
                update,
                actions,
                f"Commission rule {'activated' if rule['active'] else 'deactivated'}.",
                InlineKeyboardMarkup([[button("Commission Rules", "p3:commissions")]]),
            )
        elif op == "crnew":
            _set_state(conn, user_id, "P3_COMMISSION_RULE")
            _edit(
                update,
                actions,
                "Enter rule name | FIXED or PERCENTAGE | amount/rate | currency | project or - | agent Telegram ID or -. Example: Standard | PERCENTAGE | 2 | ETB | Riverside | -",
                InlineKeyboardMarkup([[button("Cancel", "p3:commissions")]]),
            )
        elif op == "cruleconfirm" and len(parts) == 3:
            state = _state(conn, user_id)
            payload = state.get("payload") or {} if state else {}
            if (
                not state
                or state["purpose"] != "P3_COMMISSION_RULE_CONFIRM"
                or payload.get("confirm_token") != parts[2]
            ):
                raise ValueError("Commission rule confirmation expired")
            rule = pipeline.add_commission_rule(
                conn,
                rule_name=payload["rule_name"],
                rule_type=payload["rule_type"],
                amount_or_rate=Decimal(payload["amount_or_rate"]),
                currency=payload["currency"],
                project_name=payload.get("project_name"),
                agent_id=payload.get("agent_id"),
                actor_id=user_id,
            )
            _clear_state(conn, user_id)
            _edit(
                update,
                actions,
                f"Commission rule <b>{html.escape(rule['rule_name'])}</b> created.",
                InlineKeyboardMarkup(
                    [
                        [button("View Rule", f"p3:rule:{rule['id']}")],
                        [button("Commission Rules", "p3:commissions")],
                    ]
                ),
            )
        elif op == "cmstatus" and len(parts) == 4:
            commission_id, status = UUID(parts[2]), parts[3]
            if status not in {"CONFIRMED", "PAID", "DISPUTED", "CANCELLED"}:
                raise ValueError("Unsupported commission status")
            if status == "PAID":
                _set_state(
                    conn,
                    user_id,
                    "P3_COMMISSION_PAYMENT_AMOUNT",
                    payload={"commission_id": str(commission_id)},
                )
                _edit(
                    update,
                    actions,
                    "Enter the amount received in the commission currency. Partial payments are supported.",
                    InlineKeyboardMarkup([[button("Cancel", f"p3:commission:{commission_id}")]]),
                )
                return True
            _set_state(
                conn,
                user_id,
                "P3_COMMISSION_STATUS",
                payload={"commission_id": str(commission_id), "status": status},
            )
            message = (
                "Confirm that the commission was paid according to the authorized record?"
                if status == "PAID"
                else f"Confirm recording commission status as {status.title()}?"
            )
            _edit(
                update,
                actions,
                message,
                InlineKeyboardMarkup(
                    [
                        [button("Confirm Status", f"p3:cmyes:{commission_id}:{status}")],
                        [button("Back", f"p3:commission:{commission_id}")],
                    ]
                ),
            )
        elif op == "cmyes" and len(parts) == 4:
            commission_id = UUID(parts[2])
            state = _state(conn, user_id)
            payload = state.get("payload") or {} if state else {}
            if (
                not state
                or state["purpose"] != "P3_COMMISSION_STATUS"
                or payload.get("commission_id") != str(commission_id)
                or payload.get("status") != parts[3]
            ):
                raise ValueError("Commission confirmation expired")
            pipeline.update_commission(
                conn,
                commission_id,
                status=payload["status"],
                amount=None,
                actor_id=user_id,
                confirmed=True,
                reason=payload.get("reason"),
            )
            _clear_state(conn, user_id)
            _commission_card(conn, update, actions, commission_id, user_id)
        elif op == "cmpayyes" and len(parts) == 4:
            commission_id = UUID(parts[2])
            state = _state(conn, user_id)
            payload = state.get("payload") or {} if state else {}
            if (
                not state
                or state["purpose"] != "P3_COMMISSION_PAYMENT_CONFIRM"
                or payload.get("commission_id") != str(commission_id)
                or payload.get("confirm_token") != parts[3]
            ):
                raise ValueError("Commission payment confirmation expired")
            pipeline.record_commission_payment(
                conn,
                commission_id,
                amount=Decimal(payload["amount"]),
                reference=payload["reference"],
                actor_id=user_id,
                confirmed=True,
            )
            _clear_state(conn, user_id)
            _commission_card(conn, update, actions, commission_id, user_id)
        elif op == "cmamount" and len(parts) == 3:
            commission_id = UUID(parts[2])
            _set_state(
                conn,
                user_id,
                "P3_COMMISSION_AMOUNT",
                payload={"commission_id": str(commission_id)},
            )
            _edit(
                update,
                actions,
                "Enter the manual commission amount in the sale currency.",
                InlineKeyboardMarkup([[button("Cancel", f"p3:commission:{commission_id}")]]),
            )
        elif op == "cmnew" and len(parts) == 3:
            sale_id = UUID(parts[2])
            sale = conn.execute(
                "select recorded_sale_price,currency,verification_status from sale_records where id=%s",
                (sale_id,),
            ).fetchone()
            if not sale:
                raise ValueError("Sale record not found")
            _set_state(conn, user_id, "P3_MANUAL_COMMISSION", payload={"sale_id": str(sale_id)})
            _edit(
                update,
                actions,
                f"Enter the manual commission in {sale['currency'].strip()} for this recorded sale ({sale['verification_status'].replace('_', ' ').title()}).",
                InlineKeyboardMarkup([[button("Cancel", "p3:commlist:0")]]),
            )
        elif op == "cmoneyyes" and len(parts) == 4:
            commission_id = UUID(parts[2])
            state = _state(conn, user_id)
            payload = state.get("payload") or {} if state else {}
            if (
                not state
                or state["purpose"] != "P3_COMMISSION_AMOUNT_CONFIRM"
                or payload.get("commission_id") != str(commission_id)
                or payload.get("confirm_token") != parts[3]
            ):
                raise ValueError("Manual amount confirmation expired")
            pipeline.update_commission(
                conn,
                commission_id,
                status=None,
                amount=Decimal(payload["amount"]),
                actor_id=user_id,
                confirmed=True,
                reason=payload.get("reason"),
            )
            _clear_state(conn, user_id)
            _commission_card(conn, update, actions, commission_id, user_id)
        elif op == "cmnewyes" and len(parts) == 4:
            sale_id = UUID(parts[2])
            state = _state(conn, user_id)
            payload = state.get("payload") or {} if state else {}
            if (
                not state
                or state["purpose"] != "P3_MANUAL_COMMISSION_CONFIRM"
                or payload.get("sale_id") != str(sale_id)
                or payload.get("confirm_token") != parts[3]
            ):
                raise ValueError("Manual commission confirmation expired")
            row = pipeline.record_manual_commission(
                conn, sale_id, Decimal(payload["amount"]), user_id, confirmed=True
            )
            _clear_state(conn, user_id)
            _commission_card(conn, update, actions, row["id"], user_id)
        elif op == "closing":
            rows = pipeline.command_center_sections(conn, datetime.now(UTC))["closing"]
            buttons = [
                [
                    button(
                        f"{row['name'][:22]} · {row['expected_closing_date']}", f"p3:op:{row['id']}"
                    )
                ]
                for row in rows
            ]
            buttons.extend([[button("Command Center", "p3:command")]])
            _edit(
                update,
                actions,
                "<b>Expected Closings · next 14 days</b>",
                InlineKeyboardMarkup(buttons),
            )
        elif op == "recent_sales":
            rows = pipeline.command_center_sections(conn, datetime.now(UTC))["recent_sales"]
            buttons = []
            for row in rows:
                buttons.append(
                    [
                        button(
                            f"{row['name'][:20]} · {row['currency'].strip()} {row['recorded_sale_price']:,.0f} · {row['verification_status'].replace('_', ' ').title()}",
                            f"p3:saleview:{row['id']}",
                        )
                    ]
                )
            buttons.extend([[button("Command Center", "p3:command")]])
            _edit(
                update,
                actions,
                "<b>Recent Sales</b>\nSelect a sale to review verification:",
                InlineKeyboardMarkup(buttons),
            )
        elif op == "report":
            _edit(
                update,
                actions,
                "Choose a performance reporting period:",
                InlineKeyboardMarkup(
                    [
                        [
                            button("Today", "p3:rperiod:today"),
                            button("Last 7 Days", "p3:rperiod:last7"),
                        ],
                        [button("Custom Dates", "p3:rperiod:custom")],
                        [button("Command Center", "p3:command")],
                    ]
                ),
            )
        elif op == "rperiod" and len(parts) == 3:
            period = parts[2]
            if period == "custom":
                _set_state(conn, user_id, "P3_REPORT_CUSTOM")
                _edit(
                    update,
                    actions,
                    "Send inclusive start and end dates as YYYY-MM-DD YYYY-MM-DD.",
                    InlineKeyboardMarkup([[button("Cancel", "p3:report")]]),
                )
            elif period in {"today", "last7"}:
                _summary_report(conn, update, actions, settings, period)
            else:
                raise ValueError("Choose a supported reporting period")
        elif op == "weights":
            weights = pipeline.ensure_priority_weights(conn, user_id)
            rows = [
                [button(f"{code.replace('_', ' ').title()}: {weight}", f"p3:weight:{code}")]
                for code, weight in weights.items()
            ]
            rows.extend([[button("Command Center", "p3:command")]])
            _edit(
                update,
                actions,
                "<b>Priority Weights</b>\nHigher weights appear first. Choose a rule to change its weight.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "weight" and len(parts) == 3:
            code = parts[2]
            _set_state(conn, user_id, "P3_PRIORITY_WEIGHT", payload={"code": code})
            _edit(
                update,
                actions,
                "Send a weight from 0 to 1,000.",
                InlineKeyboardMarkup([[button("Cancel", "p3:weights")]]),
            )
        elif op == "notify":
            prefs = crm.ensure_user(conn, user_id, settings)
            rows = [
                [
                    button(
                        f"Remind before · {prefs['notification_minutes_before']} min",
                        "p3:notifybefore",
                    )
                ],
                [button("Toggle Daily Overdue Summary", "p3:notifydaily")],
                [button("Command Center", "p3:command")],
            ]
            _edit(
                update,
                actions,
                f"<b>Notification Settings</b>\nDaily overdue summary: {'On' if prefs['daily_overdue_summary_enabled'] else 'Off'}\nEarly reminder lead time is stored with your CRM settings.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "notifybefore":
            rows = [
                [button(f"{minutes} minutes", f"p3:nb:{minutes}")]
                for minutes in (0, 15, 30, 60, 120, 1440)
            ]
            rows.append([button("Back", "p3:notify")])
            _edit(
                update,
                actions,
                "Choose the lead time for follow-up and visit reminders. Zero disables early reminders.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "nb" and len(parts) == 3:
            minutes = int(parts[2])
            if minutes not in {0, 15, 30, 60, 120, 1440}:
                raise ValueError("Choose a listed reminder interval")
            conn.execute(
                "update user_settings set notification_minutes_before=%s,updated_at=now() where telegram_user_id=%s",
                (minutes, user_id),
            )
            _edit(
                update,
                actions,
                "Reminder interval updated.",
                InlineKeyboardMarkup([[button("Notification Settings", "p3:notify")]]),
            )
        elif op == "notifydaily":
            conn.execute(
                "update user_settings set daily_overdue_summary_enabled=not daily_overdue_summary_enabled,updated_at=now() where telegram_user_id=%s",
                (user_id,),
            )
            _edit(
                update,
                actions,
                "Daily overdue summary setting updated.",
                InlineKeyboardMarkup([[button("Notification Settings", "p3:notify")]]),
            )
        elif op == "stageoutcome":
            raise ValueError("Unsupported action")
        else:
            return False
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        logger.info("Rejected Phase 3 callback (%s)", type(exc).__name__)
        _edit(
            update, actions, html.escape(str(exc)) or "That action is no longer valid.", main_menu()
        )
    return True


def handle_message(
    conn: Connection,
    update: Update,
    user_id: int,
    settings: Settings,
    update_id: int,
    actions: list[dict[str, Any]],
    state: dict[str, Any],
) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    text = message.text.strip()
    purpose = state["purpose"]
    payload = state.get("payload") or {}
    timezone = crm.ensure_user(conn, user_id, settings)["timezone"]
    try:
        if purpose == "P3_OPP_CONTACT":
            matches = crm.search_contacts(conn, text, min(settings.max_search_results, 8))
            select_token = _token()
            _set_state(
                conn,
                user_id,
                "P3_OPP_CONTACT_CHOOSE",
                payload={
                    "query": text[:100],
                    "contact_ids": [str(row["id"]) for row in matches],
                    "select_token": select_token,
                },
            )
            rows = [
                [
                    button(
                        f"{row['name'][:26]} · {_phone(row.get('phone_number'))}"[:58],
                        f"p3:contact:{row['id']}:{select_token}",
                    )
                ]
                for row in matches
            ]
            rows.append([button("Cancel", "p3:cancel")])
            _edit(
                update,
                actions,
                "Select the contact for this opportunity."
                if matches
                else "No contact matched. Search again or cancel.",
                InlineKeyboardMarkup(rows),
            )
        elif purpose == "P3_OPP_SEARCH":
            rows = pipeline.list_opportunities(conn, query=text, page_size=8)
            _clear_state(conn, user_id)
            buttons = [
                [button(f"{row['name'][:24]} · {row['stage_name']}", f"p3:op:{row['id']}")]
                for row in rows
            ]
            buttons.extend([[button("New Search", "p3:search")], [button("Pipeline", "p3:home")]])
            _edit(
                update,
                actions,
                "Search results:" if rows else "No opportunities matched this search.",
                InlineKeyboardMarkup(buttons),
            )
        elif purpose == "P3_FOLLOWUP_CUSTOM_TYPE":
            if not text or len(text) > 40:
                raise ValueError("Custom follow-up label must contain 1 to 40 characters")
            payload["followup_type"] = f"CUSTOM: {text}"
            _set_state(conn, user_id, "P3_FOLLOWUP_TIME", payload=payload)
            _edit(
                update,
                actions,
                f"Choose when to follow up about {html.escape(text)}.",
                _followup_time_markup(),
            )
        elif purpose == "P3_NOTE":
            opportunity_id = UUID(payload["opportunity_id"])
            pipeline.add_opportunity_note(conn, opportunity_id, text, user_id, update_id)
            _clear_state(conn, user_id)
            _opportunity_card(conn, update, actions, opportunity_id, timezone)
        elif purpose == "P3_COMMISSION_RULE":
            parts = [part.strip() for part in text.split("|", 5)]
            if len(parts) != 6:
                raise ValueError(
                    "Enter all six rule fields separated by |; use - for optional project or agent"
                )
            name, rule_type, value_text, currency, project, agent_text = parts
            rule_type = rule_type.upper()
            if rule_type not in {"FIXED", "PERCENTAGE"}:
                raise ValueError("Commission type must be FIXED or PERCENTAGE")
            amount = Decimal(value_text.replace(",", ""))
            if amount < 0 or (rule_type == "PERCENTAGE" and amount > 100):
                raise ValueError(
                    "Percentage rate must be 0 to 100; fixed amounts must be non-negative"
                )
            agent_id = None if agent_text == "-" else int(agent_text)
            if agent_id is not None and agent_id <= 0:
                raise ValueError("Agent Telegram ID must be a positive integer")
            values = {
                "rule_name": name,
                "rule_type": rule_type,
                "amount_or_rate": str(amount),
                "currency": currency.upper(),
                "project_name": None if project == "-" else project,
                "agent_id": agent_id,
            }
            values["confirm_token"] = _token()
            _set_state(conn, user_id, "P3_COMMISSION_RULE_CONFIRM", payload=values)
            amount_label = (
                f"{amount}%" if rule_type == "PERCENTAGE" else f"{currency.upper()} {amount:,.2f}"
            )
            _edit(
                update,
                actions,
                f"Create rule <b>{html.escape(name)}</b> · {rule_type.title()} {html.escape(amount_label)} · "
                f"Project {html.escape(values['project_name'] or 'All')} · "
                f"Agent {html.escape(str(agent_id) if agent_id is not None else 'All')}?",
                InlineKeyboardMarkup(
                    [
                        [button("Confirm Rule", f"p3:cruleconfirm:{values['confirm_token']}")],
                        [button("Cancel", "p3:cancel")],
                    ]
                ),
            )
        elif purpose in {"P3_COMMISSION_AMOUNT", "P3_MANUAL_COMMISSION"}:
            amount = Decimal(text.replace(",", ""))
            if amount < 0:
                raise ValueError("Commission amount cannot be negative")
            if purpose == "P3_COMMISSION_AMOUNT":
                _set_state(
                    conn,
                    user_id,
                    "P3_COMMISSION_AMOUNT_REASON",
                    payload=payload | {"amount": str(amount)},
                )
                _edit(
                    update,
                    actions,
                    "Enter a reason for this commission adjustment.",
                    InlineKeyboardMarkup(
                        [[button("Cancel", f"p3:commission:{payload['commission_id']}")]]
                    ),
                )
                return
            payload["amount"] = str(amount)
            payload["confirm_token"] = _token()
            _set_state(conn, user_id, "P3_MANUAL_COMMISSION_CONFIRM", payload=payload)
            _edit(
                update,
                actions,
                f"Confirm manual commission entry of <b>{amount:,.2f}</b> in the sale currency? This will be auditable as a manual amount.",
                InlineKeyboardMarkup(
                    [
                        [
                            button(
                                "Confirm Manual Amount",
                                f"p3:cmnewyes:{payload['sale_id']}:{payload['confirm_token']}",
                            )
                        ],
                        [button("Cancel", "p3:cancel")],
                    ]
                ),
            )
        elif purpose == "P3_COMMISSION_AMOUNT_REASON":
            if not text or len(text) > 500:
                raise ValueError("Commission adjustment reason must contain 1 to 500 characters")
            payload["reason"] = text
            payload["confirm_token"] = _token()
            _set_state(conn, user_id, "P3_COMMISSION_AMOUNT_CONFIRM", payload=payload)
            _edit(
                update,
                actions,
                f"Confirm the commission adjustment to <b>{Decimal(payload['amount']):,.2f}</b>?\nReason: {html.escape(text)}",
                InlineKeyboardMarkup(
                    [
                        [
                            button(
                                "Confirm Adjustment",
                                f"p3:cmoneyyes:{payload['commission_id']}:{payload['confirm_token']}",
                            )
                        ],
                        [button("Cancel", f"p3:commission:{payload['commission_id']}")],
                    ]
                ),
            )
        elif purpose == "P3_COMMISSION_PAYMENT_AMOUNT":
            amount = Decimal(text.replace(",", ""))
            if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
                raise ValueError("Enter a positive payment amount with no more than two decimals")
            _set_state(
                conn,
                user_id,
                "P3_COMMISSION_PAYMENT_REFERENCE",
                payload=payload | {"amount": str(amount)},
            )
            _edit(
                update,
                actions,
                "Enter the bank, receipt, or transaction reference (2–120 characters).",
                InlineKeyboardMarkup(
                    [[button("Cancel", f"p3:commission:{payload['commission_id']}")]]
                ),
            )
        elif purpose == "P3_COMMISSION_PAYMENT_REFERENCE":
            if len(text) < 2 or len(text) > 120:
                raise ValueError("Payment reference must contain 2 to 120 characters")
            payload["reference"] = text
            payload["confirm_token"] = _token()
            _set_state(conn, user_id, "P3_COMMISSION_PAYMENT_CONFIRM", payload=payload)
            _edit(
                update,
                actions,
                f"Record payment of <b>{Decimal(payload['amount']):,.2f}</b> with reference <code>{html.escape(text)}</code>? The service will reject any amount above the approved balance.",
                InlineKeyboardMarkup(
                    [
                        [
                            button(
                                "Confirm Payment",
                                f"p3:cmpayyes:{payload['commission_id']}:{payload['confirm_token']}",
                            )
                        ],
                        [button("Cancel", f"p3:commission:{payload['commission_id']}")],
                    ]
                ),
            )
        elif purpose == "P3_FOLLOWUP_CUSTOM":
            due = _datetime_local(text, timezone)
            _set_state(
                conn,
                user_id,
                "P3_FOLLOWUP_NOTE",
                payload={**payload, "scheduled_at": due.isoformat()},
            )
            _edit(
                update,
                actions,
                "Send a reason or note, or send `-` to leave it blank.",
                InlineKeyboardMarkup(
                    [[button("Skip Note", "p3:fskip")], [button("Cancel", "p3:cancel")]]
                ),
            )
        elif purpose == "P3_FOLLOWUP_NOTE":
            note = None if text == "-" else text[:2000]
            due = datetime.fromisoformat(payload["scheduled_at"])
            followup = pipeline.schedule_opportunity_followup(
                conn,
                UUID(payload["opportunity_id"]),
                due,
                payload["followup_type"],
                user_id,
                reason=note,
                notes=note,
            )
            _clear_state(conn, user_id)
            _edit(
                update,
                actions,
                f"Follow-up saved for {html.escape(local_label(followup['scheduled_at'], timezone))}.",
                InlineKeyboardMarkup(
                    [
                        [button("Opportunity", f"p3:op:{payload['opportunity_id']}")],
                        [button("Main Menu", "m:home")],
                    ]
                ),
            )
        elif purpose == "P3_FOLLOWUP_RESCHEDULE":
            due = _datetime_local(text, timezone)
            followup = pipeline.reschedule_followup(
                conn,
                UUID(payload["follow_up_id"]),
                due,
                user_id,
                update_id,
            )
            _clear_state(conn, user_id)
            _edit(
                update,
                actions,
                f"Follow-up rescheduled for {html.escape(local_label(followup['scheduled_at'], timezone))}. The previous follow-up remains in history.",
                InlineKeyboardMarkup(
                    [
                        [button("Today's Follow-Ups", "p3:followups")],
                        [button("Main Menu", "m:home")],
                    ]
                ),
            )
        elif purpose in {"P3_VISIT_TIME", "P3_VISIT_RESCHEDULE"}:
            scheduled_at = _datetime_local(text, timezone)
            payload["scheduled_at"] = scheduled_at.isoformat()
            _set_state(
                conn,
                user_id,
                "P3_VISIT_DETAILS",
                contact_id=state.get("contact_id"),
                payload=payload,
            )
            _edit(
                update,
                actions,
                "Enter meeting point | internal note | buyer instructions. Use `-` for any blank field.",
                InlineKeyboardMarkup(
                    [[button("Skip Details", "p3:vskip")], [button("Cancel", "p3:cancel")]]
                ),
            )
        elif purpose == "P3_VISIT_DETAILS":
            parts = [part.strip() for part in text.split("|", 2)]
            if len(parts) != 3:
                raise ValueError(
                    "Enter meeting point | internal note | buyer instructions, or choose Skip Details"
                )
            details = {
                "meeting_point": None if parts[0] == "-" else parts[0],
                "internal_note": None if parts[1] == "-" else parts[1],
                "buyer_instructions": None if parts[2] == "-" else parts[2],
            }
            payload["details"] = details
            _set_state(
                conn,
                user_id,
                "P3_VISIT_SAVE",
                contact_id=state.get("contact_id"),
                payload=payload,
            )
            _visit_save_prompt(update, actions, payload)
        elif purpose in {"P3_VISIT_SAVE", "P3_VISIT_DUPLICATE"}:
            prompt = (
                "Use Confirm Intentional Duplicate to continue, or cancel the workflow."
                if purpose == "P3_VISIT_DUPLICATE"
                else "Use the confirmation button to save these visit details, or cancel the workflow."
            )
            confirm = (
                "Confirm Intentional Duplicate"
                if purpose == "P3_VISIT_DUPLICATE"
                else "Confirm Visit Details"
            )
            callback = (
                f"p3:vdup:{payload['confirm_token']}"
                if purpose == "P3_VISIT_DUPLICATE"
                else f"p3:vsave:{payload['confirm_token']}"
            )
            _edit(
                update,
                actions,
                prompt,
                InlineKeyboardMarkup(
                    [[button(confirm, callback)], [button("Cancel", "p3:cancel")]]
                ),
            )
        elif purpose == "P3_VISIT_FEEDBACK":
            outcome = payload["outcome"]
            feedback = {"outcome": outcome, "notes": text[:1500]}
            liked = outcome in {"STRONG_INTEREST", "INTERESTED_WITH_CONCERNS", "PRICE_NEGOTIATION"}
            pipeline.update_visit(
                conn,
                UUID(payload["visit_id"]),
                "FEEDBACK",
                user_id,
                update_id,
                feedback=feedback,
                buyer_liked_property=liked,
                next_action="Review visit feedback and schedule the agreed next step",
            )
            _clear_state(conn, user_id)
            _visit_card(conn, update, actions, UUID(payload["visit_id"]), timezone)
        elif purpose == "P3_NEGOTIATION":
            opportunity_id = UUID(payload["opportunity_id"])
            kind = payload["kind"]
            if kind in {"buyer", "seller"}:
                amount_part, *note_parts = text.split("|", 1)
                fields = amount_part.strip().split()
                if len(fields) < 2:
                    raise ValueError("Enter amount and currency, e.g. 24,500,000 ETB")
                amount_text = fields[0].replace(",", "")
                amount = Decimal(amount_text)
                currency = fields[1].upper()
                note = note_parts[0].strip()[:1000] if note_parts else None
                if kind == "buyer":
                    values = NegotiationInput(
                        buyer_offer=amount,
                        current_discussed_price=amount,
                        currency=currency,
                        offer_date=datetime.now(ZoneInfo(timezone)).date().isoformat(),
                        event_type="INITIAL_OFFER",
                        note=note,
                    )
                else:
                    values = NegotiationInput(
                        seller_counteroffer=amount,
                        current_discussed_price=amount,
                        currency=currency,
                        offer_date=datetime.now(ZoneInfo(timezone)).date().isoformat(),
                        event_type="COUNTEROFFER",
                        note=note,
                    )
            else:
                sections = [part.strip() for part in text.split("|", 2)]
                if len(sections) < 3:
                    raise ValueError("Enter down payment | currency | payment plan")
                amount = Decimal(sections[0].replace(",", ""))
                current = conn.execute(
                    "select current_discussed_price from negotiation_records where opportunity_id=%s",
                    (opportunity_id,),
                ).fetchone()
                values = NegotiationInput(
                    down_payment=amount,
                    current_discussed_price=current["current_discussed_price"] if current else None,
                    currency=sections[1].upper(),
                    payment_plan=sections[2][:1000],
                    event_type="PAYMENT_PLAN_DISCUSSION",
                )
            pipeline.save_negotiation(conn, opportunity_id, values, user_id, update_id)
            _clear_state(conn, user_id)
            _edit(
                update,
                actions,
                "Negotiation information recorded. Earlier offers remain in the timeline.",
                InlineKeyboardMarkup(
                    [
                        [button("Timeline", f"p3:negtimeline:{opportunity_id}")],
                        [button("Opportunity", f"p3:op:{opportunity_id}")],
                    ]
                ),
            )
        elif purpose == "P3_RESERVATION":
            parts = [part.strip() for part in text.split("|")]
            if len(parts) != 3:
                raise ValueError("Enter amount | currency | reservation date (YYYY-MM-DD)")
            values = ReservationInput(
                reservation_amount=Decimal(parts[0].replace(",", "")),
                currency=parts[1].upper(),
                reservation_date=parts[2],
            )
            confirm_token = _token()
            _set_state(
                conn,
                user_id,
                "P3_RESERVATION_CONFIRM",
                payload={
                    **payload,
                    "values": values.model_dump(mode="json"),
                    "confirm_token": confirm_token,
                },
            )
            _edit(
                update,
                actions,
                f"Record reservation request for <b>{values.currency} {values.reservation_amount:,.2f}</b> on {values.reservation_date}? This does not confirm payment or reservation.",
                InlineKeyboardMarkup(
                    [
                        [
                            button(
                                "Confirm Request",
                                f"p3:rconfirm:{confirm_token}",
                            )
                        ],
                        [button("Cancel", f"p3:op:{payload['opportunity_id']}")],
                    ]
                ),
            )
        elif purpose == "P3_RESERVATION_CONFIRM":
            _edit(
                update,
                actions,
                "Use the Confirm Request button or Cancel.",
                InlineKeyboardMarkup(
                    [
                        [button("Confirm Request", f"p3:rconfirm:{payload['confirm_token']}")],
                        [button("Cancel", "p3:cancel")],
                    ]
                ),
            )
        elif purpose == "P3_SALE":
            parts = [part.strip() for part in text.split("|")]
            if len(parts) != 3:
                raise ValueError("Enter final price | currency | sale date (YYYY-MM-DD)")
            values = SaleInput(
                recorded_sale_price=Decimal(parts[0].replace(",", "")),
                currency=parts[1].upper(),
                sale_date=parts[2],
            )
            confirm_token = _token()
            _set_state(
                conn,
                user_id,
                "P3_SALE_CONFIRM",
                payload={
                    "opportunity_id": payload["opportunity_id"],
                    "sale": values.model_dump(mode="json"),
                    "confirm_token": confirm_token,
                },
            )
            _edit(
                update,
                actions,
                f"Record sale at <b>{values.currency} {values.recorded_sale_price:,.2f}</b> on {values.sale_date}? It will remain pending verification.",
                InlineKeyboardMarkup(
                    [
                        [button("Confirm Sale Record", f"p3:sconfirm:{confirm_token}")],
                        [button("Cancel", f"p3:op:{payload['opportunity_id']}")],
                    ]
                ),
            )
        elif purpose == "P3_LOST_EXPLANATION":
            payload["explanation"] = None if text == "-" else text[:1000]
            payload["confirm_token"] = _token()
            _set_state(conn, user_id, "P3_LOST_CONFIRM", payload=payload)
            _edit(
                update,
                actions,
                f"Close as lost · {LOST_LABELS[payload['reason']]}? Choose whether this opportunity may be considered for future follow-up. Do Not Contact restrictions remain in force.",
                InlineKeyboardMarkup(
                    [
                        [
                            button(
                                "Keep Eligible for Future Follow-Up",
                                f"p3:lyes:{payload['opportunity_id']}:1:{payload['confirm_token']}",
                            )
                        ],
                        [
                            button(
                                "No Future Follow-Up",
                                f"p3:lyes:{payload['opportunity_id']}:0:{payload['confirm_token']}",
                            )
                        ],
                        [button("Cancel", f"p3:op:{payload['opportunity_id']}")],
                    ]
                ),
            )
        elif purpose == "P3_REACT_FOLLOWUP":
            scheduled_at = _datetime_local(text, timezone)
            contact_id = state.get("contact_id")
            opportunity_id = (
                UUID(payload["opportunity_id"]) if payload.get("opportunity_id") else None
            )
            recommendation = conn.execute(
                """select r.status,c.contact_eligibility,c.lead_status,c.conversion_state,
                          c.normalized_phone from reactivation_recommendations r
                   join contacts c on c.id=r.contact_id where r.id=%s for update of r,c""",
                (UUID(payload["recommendation_id"]),),
            ).fetchone()
            if not recommendation or recommendation["status"] != "OPEN":
                raise ValueError("Suggestion is no longer open")
            if not pipeline.contact_is_callable(recommendation):
                raise ValueError("This contact is not eligible for an outbound follow-up")
            if opportunity_id:
                pipeline.schedule_opportunity_followup(
                    conn,
                    opportunity_id,
                    scheduled_at,
                    "CALL_BACK",
                    user_id,
                    reason="Reactivation follow-up",
                )
            else:
                pipeline.schedule_contact_followup(
                    conn,
                    contact_id,
                    scheduled_at,
                    "CALL_BACK",
                    user_id,
                    reason="Reactivation follow-up",
                )
            conn.execute(
                "update reactivation_recommendations set status='SCHEDULED',acted_at=now() where id=%s and status='OPEN'",
                (UUID(payload["recommendation_id"]),),
            )
            _clear_state(conn, user_id)
            _edit(
                update,
                actions,
                f"Follow-up scheduled for {html.escape(local_label(scheduled_at, timezone))}.",
                InlineKeyboardMarkup(
                    [[button("Reactivation Queue", "p3:react:0")], [button("Main Menu", "m:home")]]
                ),
            )
        elif purpose == "P3_PRIORITY_WEIGHT":
            weight = int(text)
            pipeline.set_priority_weight(conn, user_id, payload["code"], weight)
            _clear_state(conn, user_id)
            weights = pipeline.ensure_priority_weights(conn, user_id)
            rows = [
                [button(f"{code.replace('_', ' ').title()}: {value}", f"p3:weight:{code}")]
                for code, value in weights.items()
            ]
            rows.append([button("Command Center", "p3:command")])
            _edit(update, actions, "Priority weight updated.", InlineKeyboardMarkup(rows))
        elif purpose == "P3_REPORT_CUSTOM":
            parts = text.split()
            if len(parts) != 2:
                raise ValueError("Enter two dates as YYYY-MM-DD YYYY-MM-DD")
            custom_start, custom_end = date.fromisoformat(parts[0]), date.fromisoformat(parts[1])
            if custom_start > custom_end or (custom_end - custom_start).days > 365:
                raise ValueError("Choose an ordered range of no more than 366 days")
            if custom_end > datetime.now(ZoneInfo(timezone)).date():
                raise ValueError("Report end date cannot be in the future")
            _clear_state(conn, user_id)
            _summary_report(
                conn,
                update,
                actions,
                settings,
                period="custom",
                custom_start=custom_start,
                custom_end=custom_end,
            )
        else:
            _clear_state(conn, user_id)
            _edit(update, actions, "That form expired. Start again from the menu.", main_menu())
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        logger.info("Rejected Phase 3 input (%s)", type(exc).__name__)
        _edit(
            update,
            actions,
            html.escape(str(exc)),
            InlineKeyboardMarkup(
                [[button("Cancel", "p3:cancel")], [button("Main Menu", "m:home")]]
            ),
        )
