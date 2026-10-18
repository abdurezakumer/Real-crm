"""Telegram screens for Phase 2 workflows, using durable PostgreSQL input state."""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg import Connection
from pydantic import ValidationError
from telegram import InlineKeyboardMarkup, Update

from realestate_crm.config import Settings
from realestate_crm.models import PropertyInput, PropertyInterestInput
from realestate_crm.services import ai, crm, intelligence
from realestate_crm.telegram_ui import button, lead_keyboard, lead_text, main_menu

logger = logging.getLogger(__name__)

PROPERTY_FORM_FIELDS = (
    ("project_name", "Project name"),
    ("title", "Property title"),
    ("property_type", "Property type"),
    ("developer_or_seller", "Developer or seller"),
    ("location", "Location"),
    ("sub_location", "Sub-location"),
    ("price", "Price"),
    ("currency", "Currency code"),
    ("minimum_negotiable_price", "Minimum negotiable price"),
    ("bedrooms", "Bedrooms"),
    ("bathrooms", "Bathrooms"),
    ("floor_number", "Floor"),
    ("area_sqm", "Area in square metres"),
    ("available_payment_plans", "Available payment plans"),
    ("payment_plans_verified", "Are payment plans verified?"),
    ("construction_status", "Construction status"),
    ("delivery_date", "Delivery date"),
    ("availability_status", "Availability status"),
    ("description", "Property description"),
    ("internal_notes", "Internal notes"),
    ("listing_source", "Listing source"),
)

PROFILE_FIELDS = {
    "property_type": "Preferred property type",
    "preferred_location": "Preferred location",
    "accepted_locations": "Accepted alternative locations",
    "min_budget": "Minimum budget",
    "max_budget": "Maximum budget",
    "preferred_payment_plan": "Payment preference",
    "expected_timeline": "Purchase timeline",
    "purchase_purpose": "Purchase purpose",
    "bedrooms": "Bedrooms",
    "min_area_sqm": "Preferred size",
    "floor_preference": "Floor preference",
    "new_or_resale": "New or resale",
    "additional_requirements": "Additional requirements",
}
PROFILE_CODES = {
    "property_type": "pt",
    "preferred_location": "loc",
    "accepted_locations": "alt",
    "preferred_payment_plan": "pay",
    "expected_timeline": "time",
    "purchase_purpose": "purp",
    "bedrooms": "beds",
    "size": "size",
    "floor_preference": "floor",
    "new_or_resale": "cond",
    "additional_requirements": "req",
    "budget": "bud",
}
QUALIFICATION_CODES = {
    "property_requirement_known": "req",
    "budget_disclosed": "bud",
    "budget_declined": "decl",
    "timeline_confirmed": "time",
    "genuine_interest": "int",
    "requested_property_visit": "visit",
}
PROFILE_VALUE_CODES = {
    "APARTMENT": "apt",
    "COMMERCIAL_SHOP": "shop",
    "OFFICE": "office",
    "HOUSE_VILLA": "house",
    "LAND": "land",
    "COMMERCIAL_BUILDING": "building",
    "OTHER": "other",
    "__UNKNOWN__": "x",
    "PERSONAL_RESIDENCE": "home",
    "INVESTMENT": "invest",
    "RENTAL_INCOME": "rent",
    "COMMERCIAL_BUSINESS": "business",
    "LAND_INVESTMENT": "landinv",
    "FULL_CASH": "cash",
    "INSTALLMENT": "install",
    "BANK_FINANCING": "bank",
    "MORTGAGE": "mortgage",
    "FLEXIBLE": "flex",
    "UNDECIDED": "undecided",
    "IMMEDIATELY": "now",
    "WITHIN_1_MONTH": "m1",
    "WITHIN_3_MONTHS": "m3",
    "WITHIN_6_MONTHS": "m6",
    "WITHIN_1_YEAR": "y1",
    "MORE_THAN_1_YEAR": "later",
    "RESEARCHING": "research",
    "NEW": "new",
    "RESALE": "resale",
    "EITHER": "either",
    "UNKNOWN": "unknown",
}
SCRIPT_TYPES = {
    "first": "first_cold_call",
    "follow": "follow_up_call",
    "interested": "interested_buyer",
    "intro": "property_introduction",
    "visit": "property_visit_invitation",
    "price": "price_inquiry",
    "payment": "payment_plan_discussion",
    "noanswer": "reengagement_no_answer",
    "longgap": "reengagement_long_gap",
}

PROPERTY_TYPES = [
    ("Apartment", "APARTMENT"),
    ("Commercial shop", "COMMERCIAL_SHOP"),
    ("Office", "OFFICE"),
    ("House or villa", "HOUSE_VILLA"),
    ("Land", "LAND"),
    ("Commercial building", "COMMERCIAL_BUILDING"),
    ("Other", "OTHER"),
]
PURPOSES = [
    ("Residence", "PERSONAL_RESIDENCE"),
    ("Investment", "INVESTMENT"),
    ("Rental income", "RENTAL_INCOME"),
    ("Business", "COMMERCIAL_BUSINESS"),
    ("Land investment", "LAND_INVESTMENT"),
    ("Other", "OTHER"),
    ("Unknown", "UNKNOWN"),
]
PAYMENTS = [
    ("Full cash", "FULL_CASH"),
    ("Installment", "INSTALLMENT"),
    ("Bank financing", "BANK_FINANCING"),
    ("Mortgage", "MORTGAGE"),
    ("Flexible", "FLEXIBLE"),
    ("Undecided", "UNDECIDED"),
    ("Unknown", "UNKNOWN"),
]
TIMELINES = [
    ("Immediately", "IMMEDIATELY"),
    ("Within 1 month", "WITHIN_1_MONTH"),
    ("Within 3 months", "WITHIN_3_MONTHS"),
    ("Within 6 months", "WITHIN_6_MONTHS"),
    ("Within 1 year", "WITHIN_1_YEAR"),
    ("More than 1 year", "MORE_THAN_1_YEAR"),
    ("Researching", "RESEARCHING"),
    ("Unknown", "UNKNOWN"),
]
OBJECTIONS = [
    ("Too expensive", "The property is too expensive."),
    ("Down payment", "I cannot pay the down payment."),
    ("Need time", "I need more time."),
    ("Discuss with family", "I need to discuss it with my family."),
    ("Comparing projects", "I am comparing other projects."),
    ("Developer trust", "I do not trust the developer."),
    ("Not ready", "I am not ready to buy."),
    ("Send information", "Send me information first."),
    ("Call later", "Call me later."),
    ("Not interested", "I am not interested."),
]


def _send(
    chat_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "send",
        "chat_id": chat_id,
        "text": text,
        "markup": markup,
        "parse_mode": parse_mode,
    }


def _edit(
    update: Update,
    actions: list[dict[str, Any]],
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    message = update.effective_message
    if update.callback_query and message:
        actions.append(
            {
                "type": "edit",
                "chat_id": message.chat_id,
                "message_id": message.message_id,
                "text": text,
                "markup": markup,
                "parse_mode": parse_mode,
            }
        )
    elif message:
        actions.append(_send(message.chat_id, text, markup, parse_mode))


def _state(
    conn: Connection,
    user_id: int,
    purpose: str,
    contact_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """insert into user_input_state (telegram_user_id,purpose,contact_id,payload,expires_at)
           values (%s,%s,%s,%s,now()+interval '60 minutes') on conflict (telegram_user_id) do update
           set purpose=excluded.purpose,contact_id=excluded.contact_id,payload=excluded.payload,
             expires_at=excluded.expires_at,updated_at=now()""",
        (user_id, purpose, contact_id, json.dumps(payload or {}, default=str)),
    )


def _clear(conn: Connection, user_id: int) -> None:
    conn.execute("delete from user_input_state where telegram_user_id=%s", (user_id,))


def _state_data(state: dict[str, Any] | None) -> dict[str, Any]:
    return (state or {}).get("payload") or {}


def _contact_screen(
    conn: Connection,
    update: Update,
    user_id: int,
    settings: Settings,
    actions: list[dict[str, Any]],
    contact_id: UUID,
) -> None:
    intelligence.recalculate_lead_score(
        conn, contact_id, intelligence.lead_weights_from_settings(settings)
    )
    contact = crm.get_contact(conn, contact_id)
    if not contact:
        _edit(update, actions, "This contact is no longer available.", main_menu())
        return
    prefs = crm.ensure_user(conn, user_id, settings)
    session = crm.current_session(conn, user_id)
    session_id = str(session["id"]) if session and session["status"] == "ACTIVE" else None
    _edit(
        update,
        actions,
        lead_text(contact, prefs["timezone"]),
        lead_keyboard(contact, session_id, bool(getattr(settings, "ai_enabled", False))),
        "HTML",
    )


def _property_profile(conn: Connection, contact_id: UUID) -> dict[str, Any]:
    return (
        conn.execute(
            "select * from contact_property_interests where contact_id=%s order by updated_at desc,id desc limit 1",
            (contact_id,),
        ).fetchone()
        or {}
    )


def _profile_summary(profile: dict[str, Any]) -> str:
    def show(key: str) -> str:
        value = profile.get(key)
        if value is None or value == "" or value == []:
            return "Unknown"
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        return str(value)

    budget = f"{show('min_budget')} – {show('max_budget')} {show('currency')}"
    return (
        "<b>Buyer property requirements</b>\n"
        f"Profile: {'Complete' if profile.get('profile_complete') else 'In progress'}\n"
        f"Type: {html.escape(show('property_type'))}\n"
        f"Preferred location: {html.escape(show('preferred_location'))}\n"
        f"Accepted alternatives: {html.escape(show('accepted_locations'))}\n"
        f"Budget: {html.escape(budget)}\n"
        f"Purpose: {html.escape(show('purchase_purpose'))}\n"
        f"Bedrooms: {html.escape(show('min_bedrooms') if profile.get('min_bedrooms') is not None else show('bedrooms'))}"
        f" – {html.escape(show('max_bedrooms'))}\n"
        f"Preferred size (m²): {html.escape(show('min_area_sqm'))} – {html.escape(show('max_area_sqm'))}\n"
        f"Floor: {html.escape(show('floor_preference'))}\n"
        f"Payment preference: {html.escape(show('preferred_payment_plan'))}\n"
        f"Timeline: {html.escape(show('expected_timeline'))}\n"
        f"New / resale: {html.escape(show('new_or_resale'))}\n"
        f"Other requirements: {html.escape(show('additional_requirements'))}"
    )


def _profile_keyboard(contact_id: UUID) -> InlineKeyboardMarkup:
    rows = [
        [
            button("Property type", f"p2:pe:{contact_id}:pt"),
            button("Location", f"p2:pe:{contact_id}:loc"),
        ],
        [button("Budget", f"p2:pe:{contact_id}:bud"), button("Payment", f"p2:pe:{contact_id}:pay")],
        [
            button("Timeline", f"p2:pe:{contact_id}:time"),
            button("Purpose", f"p2:pe:{contact_id}:purp"),
        ],
        [
            button("Alternatives", f"p2:pe:{contact_id}:alt"),
            button("Bedrooms", f"p2:pe:{contact_id}:beds"),
        ],
        [button("Preferred size", f"p2:pe:{contact_id}:size")],
        [
            button("Floor", f"p2:pe:{contact_id}:floor"),
            button("New / resale", f"p2:pe:{contact_id}:cond"),
        ],
        [button("Additional requirements", f"p2:pe:{contact_id}:req")],
        [button("Mark Profile Complete", f"p2:profile_complete:{contact_id}")],
        [button("Back to Contact", f"lead:{contact_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def _show_profile(
    conn: Connection, update: Update, actions: list[dict[str, Any]], contact_id: UUID
) -> None:
    _edit(
        update,
        actions,
        _profile_summary(_property_profile(conn, contact_id)),
        _profile_keyboard(contact_id),
        "HTML",
    )


def _qualification_screen(
    conn: Connection, update: Update, user_id: int, actions: list[dict[str, Any]], contact_id: UUID
) -> None:
    data = intelligence.qualification(conn, contact_id)
    checks = [
        ("Property requirement recorded", data["property_requirement_known"]),
        ("Budget recorded or declined", data["budget_disclosed"] or data["budget_declined"]),
        ("Purchase timeline asked", data["timeline_confirmed"]),
        ("Genuine interest expressed", data["genuine_interest"]),
        ("Concrete next action", bool(data.get("next_action"))),
    ]
    lines = [
        f"<b>Buyer qualification · {html.escape(str(data.get('qualification_status') or 'UNQUALIFIED'))}</b>"
    ]
    lines.extend(f"{'✓' if complete else '○'} {label}" for label, complete in checks)
    if data.get("next_action"):
        lines.append(f"Next action: {html.escape(data['next_action'])}")
    if data.get("expected_timeline"):
        lines.append(f"Recorded timeline: {html.escape(str(data['expected_timeline']))}")
    rows = [
        [
            button(
                "Edit property requirements"
                if data["property_requirement_known"]
                else "Requirement known",
                f"p2:requirements:{contact_id}",
            )
        ],
        [
            button(
                "Edit recorded budget" if data["budget_disclosed"] else "Record budget",
                f"p2:requirements:{contact_id}",
            )
        ]
        if data["budget_disclosed"]
        else [
            button(
                "Budget declined" if data["budget_declined"] else "Mark budget declined",
                f"p2:qs:{contact_id}:decl:{int(not data['budget_declined'])}",
            )
        ],
        [
            button(
                "Timeline asked / unknown confirmed",
                f"p2:qs:{contact_id}:time:{int(not data['timeline_confirmed'])}",
            )
        ],
        [
            button(
                "Interest expressed", f"p2:qs:{contact_id}:int:{int(not data['genuine_interest'])}"
            )
        ],
        [button("Set next action", f"p2:qaction:{contact_id}")],
        [
            button(
                "Property visit requested",
                f"p2:qs:{contact_id}:visit:{int(not data['requested_property_visit'])}",
            )
        ],
        [button("Review and Confirm Status", f"p2:qreview:{contact_id}")],
        [button("Disqualify Buyer", f"p2:qdisqualify:{contact_id}")],
        [button("Back to Contact", f"lead:{contact_id}")],
    ]
    _edit(update, actions, "\n".join(lines), InlineKeyboardMarkup(rows), "HTML")


def _profile_values(profile: dict[str, Any]) -> dict[str, Any]:
    allowed = set(PropertyInterestInput.model_fields)
    return {key: profile[key] for key in allowed if key in profile}


def _save_profile(
    conn: Connection,
    contact_id: UUID,
    values: dict[str, Any],
    settings: Settings | None = None,
    user_id: int | None = None,
) -> None:
    validated = PropertyInterestInput.model_validate(values)
    weights = intelligence.lead_weights_from_settings(settings) if settings else None
    crm.save_property_interest(
        conn,
        contact_id,
        validated.model_dump(),
        score_weights=weights,
        user_id=user_id,
    )


def _property_form_prompt(
    conn: Connection,
    update: Update,
    user_id: int,
    actions: list[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    index = int(payload.get("step", 0))
    if index >= len(PROPERTY_FORM_FIELDS):
        try:
            validated = PropertyInput.model_validate(payload.get("values") or {})
        except (ValidationError, ValueError):
            _clear(conn, user_id)
            _edit(
                update,
                actions,
                "Property details did not validate. Start again from Property Inventory.",
            )
            return
        data = validated.model_dump(mode="json")
        payload.update({"values": data, "step": index, "property_id": payload.get("property_id")})
        _state(conn, user_id, "P2_PROPERTY_CONFIRM", payload=payload)
        lines = ["<b>Confirm property record</b>"]
        for key, label in PROPERTY_FORM_FIELDS:
            value = data.get(key)
            if value not in (None, "", []):
                lines.append(f"{html.escape(label)}: {html.escape(str(value))}")
        rows = [[button("Save Property", "p2:property_save")], [button("Cancel", "p2:cancel")]]
        _edit(update, actions, "\n".join(lines), InlineKeyboardMarkup(rows), "HTML")
        return
    field, label = PROPERTY_FORM_FIELDS[index]
    if field == "property_type":
        rows = [[button(label, f"p2:pv:{value}")] for label, value in PROPERTY_TYPES]
        rows.append([button("Cancel", "p2:cancel")])
        _edit(update, actions, "Select the property type.", InlineKeyboardMarkup(rows))
        return
    choices: dict[str, list[tuple[str, str]]] = {
        "payment_plans_verified": [("Yes, verified", "true"), ("No / unknown", "false")],
        "construction_status": [
            ("Under construction", "UNDER_CONSTRUCTION"),
            ("Near completion", "NEAR_COMPLETION"),
            ("Ready", "READY_FOR_DELIVERY"),
            ("Completed", "COMPLETED"),
            ("Unknown", "UNKNOWN"),
        ],
        "availability_status": [
            ("Available", "AVAILABLE"),
            ("Reserved", "RESERVED"),
            ("Sold", "SOLD"),
            ("Temporarily unavailable", "TEMPORARILY_UNAVAILABLE"),
            ("Unverified", "UNVERIFIED"),
        ],
    }
    if field in choices:
        rows = [[button(name, f"p2:pv:{value}")] for name, value in choices[field]]
        rows.append([button("Cancel", "p2:cancel")])
        _edit(update, actions, f"{label}?", InlineKeyboardMarkup(rows))
        return
    prompt = f"{label}? Send `-` if unknown."
    if field == "title":
        prompt = "Property title? A title is required. Send `-` to cancel."
    elif field == "currency":
        prompt = "Currency code? Use a three-letter code such as ETB or USD."
    elif field in {"price", "minimum_negotiable_price", "area_sqm"}:
        prompt = f"{label}? Enter a non-negative number, or `-` if unknown."
    elif field in {"bedrooms", "bathrooms"}:
        prompt = f"{label}? Enter a whole number or `-` if unknown."
    elif field == "delivery_date":
        prompt = "Estimated delivery date? Use YYYY-MM-DD, or `-` if unknown."
    elif field == "available_payment_plans":
        prompt = "List verified available payment plans separated by commas, or `-` if unknown."
    _state(conn, user_id, "P2_PROPERTY_FORM", payload=payload)
    _edit(
        update, actions, prompt, InlineKeyboardMarkup([[button("Cancel", "p2:cancel")]]), "Markdown"
    )


def _start_property_form(
    conn: Connection,
    update: Update,
    user_id: int,
    actions: list[dict[str, Any]],
    existing: dict[str, Any] | None = None,
) -> None:
    values = {}
    property_id = None
    if existing:
        property_id = str(existing["id"])
        for key, _ in PROPERTY_FORM_FIELDS:
            value = existing.get(key)
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            values[key] = value
    payload = {"step": 0, "values": values, "property_id": property_id}
    _state(conn, user_id, "P2_PROPERTY_FORM", payload=payload)
    _property_form_prompt(conn, update, user_id, actions, payload)


def _inventory_menu(update: Update, actions: list[dict[str, Any]]) -> None:
    _edit(
        update,
        actions,
        "<b>Property Inventory</b>",
        InlineKeyboardMarkup(
            [
                [
                    button("Add Property", "p2:add_property"),
                    button("Search Properties", "p2:search_properties"),
                ],
                [
                    button("View Properties", "p2:list_properties"),
                    button("Update Availability", "p2:availability_search"),
                ],
                [button("Find Matching Buyers", "p2:find_buyers")],
                [button("Property Performance", "p2:property_performance")],
                [button("Back", "m:tools")],
            ]
        ),
        "HTML",
    )


def _sales_tools(update: Update, actions: list[dict[str, Any]], ai_enabled: bool) -> None:
    rows = [
        [button("Property Inventory", "m:properties")],
        [
            button("Find Matching Properties", "p2:find_properties"),
            button("Find Matching Buyers", "p2:find_buyers"),
        ],
        [button("Qualify Buyer", "p2:qualify_search"), button("Lead Priorities", "p2:priorities")],
        [button("Calling Queue Filters", "p2:queue_filters")],
        [button("Sales Intelligence", "p2:sales_intelligence")],
    ]
    if ai_enabled:
        rows.append([button("AI Sales Assistant", "p2:ai_menu")])
    rows.append([button("Back", "m:home")])
    _edit(update, actions, "Sales tools", InlineKeyboardMarkup(rows))


def _property_list(
    update: Update,
    actions: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    title: str,
    operation: str = "detail",
) -> None:
    buttons = []
    for row in rows:
        status = row.get("availability_status", "UNVERIFIED")
        price = (
            "Unknown price"
            if row.get("price") is None
            else f"{row['price']} {row.get('currency') or ''}"
        )
        label = f"{row['title'][:25]} · {price} · {status}"
        buttons.append([button(label[:60], f"p2:prop_{operation}:{row['id']}")])
    if not buttons:
        text = "No properties found."
    else:
        text = title
    buttons.append([button("Back", "m:properties")])
    _edit(update, actions, text, InlineKeyboardMarkup(buttons))


def _property_detail(
    conn: Connection,
    update: Update,
    actions: list[dict[str, Any]],
    property_id: UUID,
    contact_id: UUID | None = None,
) -> None:
    prop = intelligence.get_property(conn, property_id)
    if not prop:
        _edit(
            update,
            actions,
            "Property not found.",
            InlineKeyboardMarkup([[button("Back", "m:properties")]]),
        )
        return
    esc = html.escape
    price = "Unknown" if prop.get("price") is None else f"{prop['price']} {prop['currency']}"
    lines = [
        f"<b>{esc(prop['title'])}</b>",
        f"Project: {esc(prop.get('project_name') or 'Unknown')}",
        f"Type: {esc(prop['property_type'])}",
        f"Developer / seller: {esc(prop.get('developer_or_seller') or 'Unknown')}",
        f"Location: {esc(prop.get('location') or 'Unknown')} · {esc(prop.get('sub_location') or 'Unknown')}",
        f"Price: {esc(price)}",
        f"Availability: <b>{esc(prop['availability_status'])}</b>",
        f"Construction: {esc(prop['construction_status'])}",
        f"Delivery: {esc(str(prop.get('delivery_date') or 'Unknown'))}",
        f"Bedrooms / bathrooms: {esc(str(prop.get('bedrooms') if prop.get('bedrooms') is not None else 'Unknown'))} / {esc(str(prop.get('bathrooms') if prop.get('bathrooms') is not None else 'Unknown'))}",
        f"Area: {esc(str(prop.get('area_sqm') if prop.get('area_sqm') is not None else 'Unknown'))} m²",
        f"Payment options: {esc(', '.join(prop.get('available_payment_plans') or []) or 'Unknown')}"
        f" {'(verified)' if prop.get('payment_plans_verified') else '(unverified)'}",
        f"Description: {esc(prop.get('description') or 'Unknown')}",
    ]
    rows = [
        [
            button("Edit Property", f"p2:edit_property:{property_id}"),
            button("Update Price", f"p2:edit_price:{property_id}"),
        ],
        [button("Update Availability", f"p2:availability:{property_id}")],
        [button("Find Matching Buyers", f"p2:property_matches:{property_id}")],
    ]
    if contact_id:
        rows.insert(0, [button("Record as Recommended", f"p2:recommend:{property_id}")])
        rows.insert(
            1,
            [
                button("Mark Buyer Interested", f"p2:interest:{property_id}"),
                button("Schedule Visit", f"p2:visit:{property_id}"),
            ],
        )
        rows.insert(2, [button("Reject Match", f"p2:reject_match:{property_id}")])
        _state(
            conn,
            update.effective_user.id,
            "P2_BUYER_CONTEXT",
            contact_id,
            {"property_id": str(property_id)},
        )
    rows.append([button("Back", "m:properties")])
    _edit(update, actions, "\n".join(lines), InlineKeyboardMarkup(rows), "HTML")


def _lead_priority_screen(conn: Connection, update: Update, actions: list[dict[str, Any]]) -> None:
    rows = intelligence.lead_priorities(conn, limit=20)
    buttons = []
    lines = ["<b>Lead priorities · explainable rules, not conversion probabilities</b>"]
    for row in rows:
        lines.append(
            f"{row['score']}/100 · Priority {html.escape(str(row['priority_category']))} · {html.escape(row['name'])}"
        )
        reasons = (row.get("explanation") or {}).get("reasons") or []
        missing = (row.get("explanation") or {}).get("missing_information") or []
        if reasons:
            lines.append("  Reasons: " + html.escape(", ".join(reasons[:3])))
        if missing:
            lines.append("  Missing: " + html.escape(", ".join(missing[:2])))
        buttons.append([button(f"Open {row['name'][:30]} · {row['score']}", f"lead:{row['id']}")])
    if not rows:
        lines.append("No eligible leads are currently scored.")
    buttons.append([button("Back", "m:tools")])
    _edit(update, actions, "\n".join(lines), InlineKeyboardMarkup(buttons), "HTML")


def _sales_report_screen(
    conn: Connection,
    update: Update,
    actions: list[dict[str, Any]],
    period: str,
    timezone: str,
    settings: Settings,
    *,
    custom_start: date | None = None,
    custom_end: date | None = None,
) -> None:
    data = intelligence.sales_intelligence_report(
        conn, period, timezone, custom_start=custom_start, custom_end=custom_end
    )
    text = (
        f"<b>Sales intelligence · {html.escape(period)}</b>\n"
        f"Period: {data['start']} to {data['end']} ({html.escape(timezone)})\n\n"
        f"Calls attempted: {data.get('attempted', 0)} · Unique prospects contacted: {data.get('unique_contacted', 0)}\n"
        f"Answered: {data.get('answered', 0)} ({data.get('answer_rate', 0)}% of attempts) · Interest: {data.get('interested', 0)} ({data.get('interest_rate', 0)}% of unique answered prospects)\n"
        f"Qualified: {data.get('qualified', 0)} · Meetings: {data.get('meetings', 0)} · Visits: {data.get('visits', 0)} · Conversions: {data.get('conversions', 0)}\n"
        f"Follow-ups: {data.get('scheduled', 0)} scheduled · {data.get('completed', 0)} completed · {data.get('missed', 0)} missed"
    )
    lines = [text, "", "<b>Campaign performance</b>"]
    for row in data.get("campaigns", [])[:5]:
        lines.append(
            f"{html.escape(row['name'])}: {row['unique_contacted']} unique · {row['qualified']} qualified · {row['conversions']} converted"
        )
    if not data.get("campaigns"):
        lines.append("No campaign calls in this period.")
    lines.append("<b>Property performance</b>")
    for row in data.get("properties", [])[:5]:
        status = " · not available" if row["availability_status"] != "AVAILABLE" else ""
        lines.append(
            f"{html.escape(row['title'])}: {row['recommendations']} recommendations · {row['interested']} interested · {row['visits']} visits{status}"
        )
    if not data.get("properties"):
        lines.append("No property recommendation activity in this period.")
    buttons = [
        [button("Today", "p2:report:today"), button("Yesterday", "p2:report:yesterday")],
        [button("Last 7 days", "p2:report:last7"), button("Last 30 days", "p2:report:last30")],
        [button("Current month", "p2:report:month")],
        [button("Custom range", "p2:custom_report")],
    ]
    if getattr(settings, "ai_enabled", False) and period != "custom":
        buttons.append([button("Analyze My Sales with AI", f"p2:analyze:{period}")])
    buttons.append([button("Back", "m:tools")])
    _edit(update, actions, "\n".join(lines), InlineKeyboardMarkup(buttons), "HTML")


def _start_contact_lookup(
    conn: Connection, update: Update, user_id: int, actions: list[dict[str, Any]], operation: str
) -> None:
    _state(conn, user_id, "P2_CONTACT_SEARCH", payload={"operation": operation})
    _edit(
        update,
        actions,
        "Send the buyer's name or phone number.",
        InlineKeyboardMarkup([[button("Cancel", "p2:cancel")]]),
    )


def _contact_matches(
    conn: Connection,
    update: Update,
    user_id: int,
    settings: Settings,
    actions: list[dict[str, Any]],
    query: str,
    operation: str,
) -> None:
    matches = crm.search_contacts(conn, query, 10)
    buttons = [
        [
            button(
                f"{row['name'][:32]} · {row['phone_number'][:20]}",
                f"p2:select_contact:{operation}:{row['id']}",
            )
        ]
        for row in matches
    ]
    buttons.append([button("Cancel", "p2:cancel")])
    _edit(
        update,
        actions,
        "Select a contact." if matches else "No contacts matched that search.",
        InlineKeyboardMarkup(buttons),
    )


def _buyer_property_matches(
    conn: Connection,
    update: Update,
    user_id: int,
    actions: list[dict[str, Any]],
    contact_id: UUID,
    settings: Settings,
) -> None:
    matches = intelligence.find_matching_properties(
        conn,
        contact_id,
        limit=10,
        allow_near_budget=getattr(settings, "match_allow_near_budget", False),
        weights=intelligence.match_weights_from_settings(settings),
    )
    if not matches:
        profile = _property_profile(conn, contact_id)
        eligible = crm.get_contact(conn, contact_id)
        if not _sales_actions_allowed(eligible):
            _edit(
                update,
                actions,
                "This contact is excluded from matching due to eligibility or lead status.",
                InlineKeyboardMarkup([[button("Back to Contact", f"lead:{contact_id}")]]),
            )
            return
        if not profile:
            _edit(
                update,
                actions,
                "No buyer profile is recorded yet.",
                InlineKeyboardMarkup(
                    [
                        [button("Add Requirements", f"p2:requirements:{contact_id}")],
                        [button("Back", f"lead:{contact_id}")],
                    ]
                ),
            )
            return
        _edit(
            update,
            actions,
            "No available property passed the recorded buyer constraints.",
            InlineKeyboardMarkup(
                [
                    [button("Review Requirements", f"p2:requirements:{contact_id}")],
                    [button("Back", f"lead:{contact_id}")],
                ]
            ),
        )
        return
    _state(
        conn,
        user_id,
        "P2_BUYER_CONTEXT",
        contact_id,
        {"matches": [str(item["property"]["id"]) for item in matches]},
    )
    buttons = []
    lines = ["<b>Available property matches</b>"]
    for item in matches:
        prop, match = item["property"], item["match"]
        price = (
            "Price unknown" if prop.get("price") is None else f"{prop['price']} {prop['currency']}"
        )
        lines.append(
            f"{match['score']}/100 · {html.escape(match['category'])} · {html.escape(prop['title'])} · {html.escape(price)}"
        )
        if match["reasons"]:
            lines.append("  " + html.escape("; ".join(match["reasons"][:3])))
        if match["missing_information"]:
            lines.append("  Missing: " + html.escape("; ".join(match["missing_information"][:2])))
        buttons.append(
            [button(f"View {prop['title'][:36]} · {match['score']}", f"p2:bprop:{prop['id']}")]
        )
    buttons.append([button("Back to Buyer", f"lead:{contact_id}")])
    _edit(update, actions, "\n".join(lines), InlineKeyboardMarkup(buttons), "HTML")


def _property_buyer_matches(
    conn: Connection,
    update: Update,
    user_id: int,
    actions: list[dict[str, Any]],
    property_id: UUID,
    settings: Settings,
) -> None:
    prop = intelligence.get_property(conn, property_id)
    if not prop:
        _edit(update, actions, "Property not found.")
        return
    if prop["availability_status"] != "AVAILABLE":
        _edit(
            update,
            actions,
            f"This property is marked {prop['availability_status']}. It will not be recommended as available.",
            InlineKeyboardMarkup([[button("Back", f"p2:prop_detail:{property_id}")]]),
        )
        return
    matches = intelligence.find_matching_buyers(
        conn,
        property_id,
        limit=10,
        allow_near_budget=getattr(settings, "match_allow_near_budget", False),
        weights=intelligence.match_weights_from_settings(settings),
    )
    lines = [f"<b>Buyer matches for {html.escape(prop['title'])}</b>"]
    buttons = []
    records: dict[str, str] = {}
    for item in matches:
        buyer, match = item["buyer"], item["match"]
        rec = intelligence.save_recommendation(conn, buyer["id"], property_id, match)
        records[str(buyer["id"])] = str(rec["id"])
        lines.append(
            f"\n{html.escape(buyer['name'])} · {html.escape(str(buyer['lead_status']))} · {match['score']}/100"
        )
        lines.append(
            f"Phone: {html.escape(buyer['phone_number'])} · Budget: {html.escape(str(buyer.get('min_budget') or 'Unknown'))}–{html.escape(str(buyer.get('max_budget') or 'Unknown'))} {html.escape(str(buyer.get('currency') or ''))}"
        )
        lines.append(f"Location: {html.escape(str(buyer.get('preferred_location') or 'Unknown'))}")
        if match["reasons"]:
            lines.append("Reasons: " + html.escape("; ".join(match["reasons"][:3])))
        if match["missing_information"]:
            lines.append("Missing: " + html.escape("; ".join(match["missing_information"][:2])))
        buttons.append(
            [
                button(f"View {buyer['name'][:24]}", f"lead:{buyer['id']}"),
                button("Call", f"call:{buyer['id']}"),
            ]
        )
        buttons.append(
            [
                button("Schedule Follow-Up", f"f:{buyer['id']}"),
                button("Record Interest", f"p2:buyer_interest:{buyer['id']}"),
            ]
        )
        buttons.append([button("Reject Match", f"p2:buyer_reject:{buyer['id']}")])
    if not matches:
        lines.append("No eligible buyers match the recorded preferences.")
    _state(
        conn,
        user_id,
        "P2_MATCH_CONTEXT",
        payload={"property_id": str(property_id), "recommendations": records},
    )
    buttons.append([button("Back to Property", f"p2:prop_detail:{property_id}")])
    _edit(update, actions, "\n".join(lines), InlineKeyboardMarkup(buttons), "HTML")


def _qualification_review(
    conn: Connection, update: Update, actions: list[dict[str, Any]], contact_id: UUID
) -> None:
    data = intelligence.qualification(conn, contact_id)
    predicted = intelligence.derive_qualification(data)["status"]
    missing = intelligence.derive_qualification(data)["missing"]
    text = f"<b>Qualification review</b>\nProposed status: {html.escape(predicted)}"
    if missing:
        text += "\nMissing: " + html.escape(", ".join(missing))
    _edit(
        update,
        actions,
        text,
        InlineKeyboardMarkup(
            [
                [
                    button(
                        f"Confirm {predicted.replace('_', ' ').title()}",
                        f"p2:qconfirm:{contact_id}",
                    )
                ],
                [button("Back to Checklist", f"p2:qualify:{contact_id}")],
            ]
        ),
        "HTML",
    )


def _queue_filter_screen(
    conn: Connection,
    update: Update,
    user_id: int,
    settings: Settings,
    actions: list[dict[str, Any]],
) -> None:
    prefs = crm.ensure_user(conn, user_id, settings)
    filters = prefs.get("queue_filters") or {}
    labels = {
        "lead_status": "Lead status",
        "priority_category": "Lead priority",
        "property_type": "Property type",
        "preferred_location": "Preferred location",
        "budget": "Budget range",
        "followup_status": "Follow-up status",
    }
    rows = []
    for key, label in labels.items():
        if key == "budget":
            value = (
                "Unknown"
                if filters.get("min_budget") is None and filters.get("max_budget") is None
                else f"{filters.get('min_budget') or '—'} to {filters.get('max_budget') or '—'} {filters.get('currency') or ''}"
            )
        else:
            value = filters.get(key) or "Any"
        rows.append([button(f"{label}: {str(value)[:20]}", f"p2:qf:{key}")])
        if value != "Any":
            rows.append([button(f"Clear {label}", f"p2:qfc:{key}")])
    rows.extend([[button("Clear all filters", "p2:qfclear")], [button("Back", "m:tools")]])
    _edit(
        update,
        actions,
        "Filters apply to the existing calling queue and are stored in your database settings.",
        InlineKeyboardMarkup(rows),
    )


def _ai_context(
    conn: Connection, contact_id: UUID, include_name: bool = False
) -> tuple[dict[str, Any] | None, str | None]:
    contact = crm.get_contact(conn, contact_id)
    if not contact:
        return None, "Contact not found."
    if not _sales_actions_allowed(contact):
        return (
            None,
            "AI assistance is unavailable for this contact's current eligibility or status.",
        )
    profile = _property_profile(conn, contact_id)
    notes = crm.lead_history(conn, contact_id)
    context: dict[str, Any] = {
        "campaign": contact.get("campaign_name"),
        "lead_status": contact.get("lead_status"),
        "buyer_preferences": {
            key: profile.get(key)
            for key in (
                "property_type",
                "preferred_location",
                "accepted_locations",
                "min_budget",
                "max_budget",
                "currency",
                "bedrooms",
                "min_bedrooms",
                "max_bedrooms",
                "preferred_payment_plan",
                "expected_timeline",
                "purchase_purpose",
                "additional_requirements",
            )
        }
        if profile
        else {},
        "previous_call_outcomes": [
            {"category": row.get("outcome_category"), "outcome": row.get("outcome_code")}
            for row in notes[:5]
        ],
    }
    if include_name:
        context["prospect_name"] = contact["name"][:80]
    if contact.get("latest_note"):
        context["relevant_note"] = _redact_contact_numbers(contact["latest_note"][:500], contact)
    if profile:
        matches = intelligence.find_matching_properties(conn, contact_id, limit=1)
        if matches:
            prop = matches[0]["property"]
            context["verified_property"] = {
                "title": prop["title"],
                "type": prop["property_type"],
                "location": prop.get("location"),
                "price": prop.get("price"),
                "currency": prop["currency"],
                "availability": prop["availability_status"],
                "construction_status": prop["construction_status"],
                "delivery_date": prop.get("delivery_date"),
                "verified_payment_plans": prop.get("available_payment_plans")
                if prop.get("payment_plans_verified")
                else None,
            }
    return context, None


def _sales_actions_allowed(contact: dict[str, Any] | None) -> bool:
    return bool(
        contact
        and contact.get("contact_eligibility") == "ELIGIBLE"
        and contact.get("lead_status")
        not in {"DO_NOT_CONTACT", "NOT_INTERESTED", "DISQUALIFIED", "CONVERTED"}
        and contact.get("conversion_state") != "CONVERTED"
        and contact.get("qualification_status") != "DISQUALIFIED"
    )


def _redact_contact_numbers(text: str, contact: dict[str, Any]) -> str:
    for phone in (contact.get("phone_number"), contact.get("normalized_phone")):
        digits = re.sub(r"\D", "", str(phone or ""))
        if len(digits) < 8:
            continue
        number = r"[+]?\s*" + r"[\s().-]*".join(digits) + r"(?!\d)"
        text = re.sub(r"(?<!\d)" + number, "[phone removed]", text)
    return text


def _queue_ai(
    conn: Connection,
    update: Update,
    user_id: int,
    settings: Settings,
    actions: list[dict[str, Any]],
    operation: str,
    payload: dict[str, Any],
    contact_id: UUID | None = None,
    property_id: UUID | None = None,
) -> None:
    if not getattr(settings, "ai_enabled", False):
        _edit(update, actions, "AI assistance is disabled. Configure an AI provider to enable it.")
        return
    try:
        job = ai.queue_ai_job(
            conn,
            settings=settings,
            user_id=user_id,
            chat_id=update.effective_chat.id,
            job_type=operation,
            payload=payload,
            contact_id=contact_id,
            property_id=property_id,
        )
    except ai.AIError as exc:
        _edit(update, actions, str(exc))
        return
    _state(conn, user_id, "P2_AI_WAIT", contact_id, {"job_id": str(job["id"])})
    _edit(
        update,
        actions,
        "AI request queued. The result will arrive when the background worker runs.",
    )


def _ai_menu(update: Update, actions: list[dict[str, Any]]) -> None:
    _edit(
        update,
        actions,
        "<b>AI Sales Assistant</b> · optional, provider configured by the administrator",
        InlineKeyboardMarkup(
            [
                [button("Prepare Call Script", "p2:contact_lookup:script")],
                [button("Structure Call Notes", "p2:contact_lookup:notes")],
                [button("Help with Objection", "p2:contact_lookup:objection")],
                [button("Draft Follow-Up Message", "p2:contact_lookup:draft")],
                [button("Analyze My Sales", "p2:ai_analyze_periods")],
                [button("Back", "m:tools")],
            ]
        ),
        "HTML",
    )


def _script_choices(update: Update, actions: list[dict[str, Any]], contact_id: UUID) -> None:
    types = [
        ("First cold call", "first"),
        ("Follow-up call", "follow"),
        ("Interested buyer", "interested"),
        ("Property introduction", "intro"),
        ("Visit invitation", "visit"),
        ("Price inquiry", "price"),
        ("Payment plan", "payment"),
        ("No answer", "noanswer"),
        ("Long gap", "longgap"),
    ]
    rows = [[button(label, f"p2:sc:{contact_id}:{kind}:en")] for label, kind in types]
    rows.append([button("Amharic", f"p2:sc:{contact_id}:follow:am")])
    rows.append([button("Cancel", "p2:cancel")])
    _edit(update, actions, "Choose a script objective and language.", InlineKeyboardMarkup(rows))


def _generate_contact_script(
    conn: Connection,
    update: Update,
    user_id: int,
    settings: Settings,
    actions: list[dict[str, Any]],
    contact_id: UUID,
    kind: str,
    language: str,
) -> None:
    context, error = _ai_context(conn, contact_id)
    if error:
        _edit(
            update, actions, error, InlineKeyboardMarkup([[button("Back", f"lead:{contact_id}")]])
        )
        return
    payload = {
        "objective": kind,
        "language": "Amharic" if language == "am" else "English",
        "context": context,
        "desired_length": "brief spoken script",
    }
    _queue_ai(conn, update, user_id, settings, actions, "CALL_SCRIPT", payload, contact_id)


def _create_ai_draft(
    conn: Connection,
    update: Update,
    user_id: int,
    settings: Settings,
    actions: list[dict[str, Any]],
    contact_id: UUID,
) -> None:
    context, error = _ai_context(conn, contact_id, include_name=True)
    if error:
        _edit(
            update, actions, error, InlineKeyboardMarkup([[button("Back", f"lead:{contact_id}")]])
        )
        return
    context["requested_message_type"] = "respectful follow-up after a call"
    _queue_ai(
        conn,
        update,
        user_id,
        settings,
        actions,
        "FOLLOWUP_DRAFT",
        {"context": context},
        contact_id,
    )


def _ai_job(conn: Connection, user_id: int, job_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        "select * from ai_jobs where id=%s and telegram_user_id=%s and status='COMPLETED'",
        (job_id, user_id),
    ).fetchone()


def _save_structured_notes(
    conn: Connection, user_id: int, settings: Settings, job: dict[str, Any]
) -> None:
    if not job.get("contact_id"):
        raise ValueError("This note review is not linked to a contact")
    output = ai.StructuredNotesOutput.model_validate(job.get("result_payload") or {})
    contact_id = job["contact_id"]
    original = (job.get("input_payload") or {}).get(
        "source_note", (job.get("input_payload") or {}).get("notes", "")
    )
    crm.save_note(conn, contact_id, original, "AI_STRUCTURED_SOURCE")
    profile = _property_profile(conn, contact_id)
    values = _profile_values(profile)
    updates = output.model_dump(exclude_none=True)
    for key in (
        "property_type",
        "preferred_location",
        "accepted_locations",
        "min_budget",
        "max_budget",
        "currency",
        "purchase_purpose",
        "bedrooms",
        "preferred_payment_plan",
        "expected_timeline",
    ):
        if key in updates:
            values[key] = updates[key]
    if updates.get("bedrooms") is not None:
        values["min_bedrooms"] = updates["bedrooms"]
        values["max_bedrooms"] = updates["bedrooms"]
    if updates.get("currency") is None and (
        updates.get("min_budget") is not None or updates.get("max_budget") is not None
    ):
        values["currency"] = profile.get("currency") or "ETB"
    _save_profile(conn, contact_id, values, settings, user_id)
    qualification_updates = {
        "property_requirement_known": bool(
            values.get("property_type") or values.get("preferred_location")
        ),
        "budget_disclosed": bool(
            values.get("min_budget") is not None or values.get("max_budget") is not None
        ),
        "timeline_confirmed": bool(values.get("expected_timeline")),
    }
    if output.budget_declined is not None:
        qualification_updates["budget_declined"] = output.budget_declined
        if output.budget_declined:
            qualification_updates["budget_disclosed"] = False
    if output.genuine_interest is not None:
        qualification_updates["genuine_interest"] = output.genuine_interest
    intelligence.save_qualification(
        conn,
        contact_id,
        user_id,
        qualification_updates,
        score_weights=intelligence.lead_weights_from_settings(settings),
    )
    if output.followup_requested:
        crm.save_note(
            conn,
            contact_id,
            "Follow-up requested in call notes; confirm an exact date and time before scheduling.",
            "FOLLOWUP_NEEDS_CONFIRMATION",
        )
    conn.execute(
        """update ai_jobs set status='CANCELLED',input_payload='{}'::jsonb,
             result_payload='{}'::jsonb,updated_at=now() where id=%s and telegram_user_id=%s""",
        (job["id"], user_id),
    )


def _format_followup_output(
    update: Update, actions: list[dict[str, Any]], job: dict[str, Any], job_id: UUID
) -> None:
    result = job.get("result_payload") or {}
    content = result.get("message", "")
    if result.get("do_not_send"):
        markup = InlineKeyboardMarkup(
            [[button("Save Draft", f"ai:save:{job_id}"), button("Cancel", f"ai:close:{job_id}")]]
        )
        content += (
            "\n\nThis output is marked do not send because the contact declined further contact."
        )
    else:
        markup = InlineKeyboardMarkup(
            [
                [button("Copy Message", f"ai:copy:{job_id}"), button("Edit", f"ai:edit:{job_id}")],
                [
                    button("Save as Note", f"ai:save:{job_id}"),
                    button("Mark as Sent", f"ai:sent:{job_id}"),
                ],
                [button("Cancel", f"ai:close:{job_id}")],
            ]
        )
    _edit(update, actions, content, markup)


def handle_callback(
    conn: Connection,
    update: Update,
    user_id: int,
    settings: Settings,
    update_id: int,
    actions: list[dict[str, Any]],
    data: str,
) -> bool:
    """Handle p2/ai callbacks. Return false for callbacks owned by Phase 1."""
    parts = data.split(":")
    if not parts or parts[0] not in {"p2", "ai"}:
        return False
    query = update.callback_query
    if not query:
        return False
    actions.append({"type": "answer", "callback_id": query.id, "text": ""})
    prefix = parts[0]
    operation = parts[1] if len(parts) > 1 else ""
    try:
        if prefix == "p2" and operation == "cancel":
            state = conn.execute(
                "select purpose,contact_id from user_input_state where telegram_user_id=%s",
                (user_id,),
            ).fetchone()
            _clear(conn, user_id)
            if state and state.get("purpose") == "P2_PROFILE_INPUT" and state.get("contact_id"):
                _show_profile(conn, update, actions, state["contact_id"])
            elif (
                state
                and state.get("purpose")
                in {
                    "P2_AI_NOTES",
                    "P2_AI_DRAFT_EDIT",
                    "P2_QACTION",
                    "P2_VISIT",
                    "P2_RECOMMENDATION_REJECT",
                }
                and state.get("contact_id")
            ):
                _contact_screen(conn, update, user_id, settings, actions, state["contact_id"])
            elif state and state.get("purpose") == "P2_QUEUE_FILTER_INPUT":
                _queue_filter_screen(conn, update, user_id, settings, actions)
            elif state and (
                str(state.get("purpose", "")).startswith("P2_PROPERTY")
                or state.get("purpose") == "P2_PRICE_CONFIRM"
            ):
                _inventory_menu(update, actions)
            else:
                _edit(update, actions, "Cancelled.", main_menu())
        elif prefix == "p2" and operation == "menu":
            _sales_tools(update, actions, bool(getattr(settings, "ai_enabled", False)))
        elif prefix == "p2" and operation == "inventory":
            _inventory_menu(update, actions)
        elif prefix == "p2" and operation == "ai_menu":
            if getattr(settings, "ai_enabled", False):
                _ai_menu(update, actions)
            else:
                _edit(
                    update,
                    actions,
                    "AI is disabled. Core CRM features are available without AI.",
                    InlineKeyboardMarkup([[button("Back", "m:tools")]]),
                )
        elif prefix == "p2" and operation == "sales_intelligence":
            _edit(
                update,
                actions,
                "Choose a reporting period.",
                InlineKeyboardMarkup(
                    [
                        [
                            button("Today", "p2:report:today"),
                            button("Yesterday", "p2:report:yesterday"),
                        ],
                        [
                            button("Last 7 days", "p2:report:last7"),
                            button("Last 30 days", "p2:report:last30"),
                        ],
                        [
                            button("Current month", "p2:report:month"),
                            button("Custom range", "p2:custom_report"),
                        ],
                        [button("Back", "m:tools")],
                    ]
                ),
            )
        elif prefix == "p2" and operation == "report" and len(parts) == 3:
            prefs = crm.ensure_user(conn, user_id, settings)
            _sales_report_screen(conn, update, actions, parts[2], prefs["timezone"], settings)
        elif prefix == "p2" and operation == "custom_report":
            _state(conn, user_id, "P2_CUSTOM_REPORT")
            _edit(
                update,
                actions,
                "Enter start and end dates as YYYY-MM-DD YYYY-MM-DD.",
                InlineKeyboardMarkup([[button("Cancel", "p2:cancel")]]),
            )
        elif prefix == "p2" and operation == "priorities":
            _lead_priority_screen(conn, update, actions)
        elif prefix == "p2" and operation == "queue_filters":
            _queue_filter_screen(conn, update, user_id, settings, actions)
        elif prefix == "p2" and operation == "qf" and len(parts) == 3:
            key = parts[2]
            choices: dict[str, list[tuple[str, str]]] = {
                "lead_status": [
                    (value, value)
                    for value in (
                        "NEW",
                        "CONTACTED",
                        "INTERESTED",
                        "QUALIFIED",
                        "FOLLOW_UP_REQUIRED",
                        "MEETING_SCHEDULED",
                        "PROPERTY_VISIT_SCHEDULED",
                        "NEGOTIATION",
                    )
                ],
                "priority_category": [(value, value) for value in ("A", "B", "C", "NEEDS_INFO")],
                "property_type": [(label, value) for label, value in PROPERTY_TYPES],
                "followup_status": [
                    (label, value)
                    for label, value in (
                        ("Due / overdue", "due"),
                        ("Future callback", "future"),
                        ("Overdue", "overdue"),
                        ("No pending follow-up", "none"),
                    )
                ],
            }
            if key in choices:
                rows = [[button(label, f"p2:qfv:{key}:{value}")] for label, value in choices[key]]
                rows.append([button("Clear filter", f"p2:qfc:{key}")])
                rows.append([button("Back", "p2:queue_filters")])
                _edit(
                    update, actions, f"Choose {key.replace('_', ' ')}.", InlineKeyboardMarkup(rows)
                )
            elif key in {"preferred_location", "budget"}:
                _state(conn, user_id, "P2_QUEUE_FILTER_INPUT", payload={"key": key})
                prompt = "Send a preferred buyer location."
                if key == "budget":
                    prompt = "Send minimum maximum currency, for example 500000 3000000 ETB. Use `-` for an unbounded side."
                _edit(
                    update, actions, prompt, InlineKeyboardMarkup([[button("Cancel", "p2:cancel")]])
                )
            else:
                raise ValueError("Unsupported calling queue filter")
        elif prefix == "p2" and operation == "qfv" and len(parts) == 4:
            key, value = parts[2], parts[3]
            if key not in {"lead_status", "priority_category", "property_type", "followup_status"}:
                raise ValueError("Unsupported calling queue filter")
            crm.ensure_user(conn, user_id, settings)
            crm.save_queue_filters(conn, user_id, {key: value})
            _queue_filter_screen(conn, update, user_id, settings, actions)
        elif prefix == "p2" and operation == "qfc" and len(parts) == 3:
            crm.ensure_user(conn, user_id, settings)
            crm.clear_queue_filter(conn, user_id, parts[2])
            _queue_filter_screen(conn, update, user_id, settings, actions)
        elif prefix == "p2" and operation == "qfclear":
            crm.ensure_user(conn, user_id, settings)
            crm.clear_queue_filter(conn, user_id)
            _queue_filter_screen(conn, update, user_id, settings, actions)
        elif prefix == "p2" and operation in {"find_properties", "qualify_search", "find_buyers"}:
            if operation == "find_buyers":
                _property_list(
                    update,
                    actions,
                    intelligence.search_properties(conn, available_only=True, limit=20),
                    "Available properties",
                )
            else:
                _start_contact_lookup(
                    conn,
                    update,
                    user_id,
                    actions,
                    "findprops" if operation == "find_properties" else "qualify",
                )
        elif prefix == "p2" and operation == "contact_lookup" and len(parts) == 3:
            if not getattr(settings, "ai_enabled", False):
                _edit(update, actions, "AI is disabled.")
            else:
                _start_contact_lookup(conn, update, user_id, actions, parts[2])
        elif prefix == "p2" and operation == "ai_analyze_periods":
            _edit(
                update,
                actions,
                "Choose the period to analyze.",
                InlineKeyboardMarkup(
                    [
                        [
                            button("Today", "p2:analyze:today"),
                            button("Yesterday", "p2:analyze:yesterday"),
                        ],
                        [
                            button("Last 7 days", "p2:analyze:last7"),
                            button("Last 30 days", "p2:analyze:last30"),
                        ],
                        [button("Current month", "p2:analyze:month")],
                        [button("Back", "p2:ai_menu")],
                    ]
                ),
            )
        elif prefix == "p2" and operation == "analyze" and len(parts) == 3:
            if not getattr(settings, "ai_enabled", False):
                _edit(update, actions, "AI is disabled.")
            else:
                prefs = crm.ensure_user(conn, user_id, settings)
                report = intelligence.sales_intelligence_report(conn, parts[2], prefs["timezone"])
                _queue_ai(
                    conn,
                    update,
                    user_id,
                    settings,
                    actions,
                    "SALES_ANALYSIS",
                    {
                        "period": report["period"],
                        "start": report["start"],
                        "end": report["end"],
                        "metrics": {
                            key: report.get(key)
                            for key in (
                                "attempted",
                                "unique_contacted",
                                "answered",
                                "answered_contacts",
                                "answer_rate",
                                "interest_rate",
                                "interested",
                                "qualified",
                                "meetings",
                                "visits",
                                "conversions",
                                "scheduled",
                                "completed",
                                "missed",
                            )
                        },
                        "campaigns": [
                            {
                                key: campaign.get(key)
                                for key in (
                                    "name",
                                    "unique_contacted",
                                    "qualified",
                                    "qualified_rate",
                                    "conversions",
                                    "conversion_rate",
                                )
                            }
                            for campaign in report.get("campaigns", [])[:5]
                        ],
                        "property_performance": [
                            {
                                key: property_row.get(key)
                                for key in (
                                    "title",
                                    "availability_status",
                                    "recommendations",
                                    "interested",
                                    "visits",
                                )
                            }
                            for property_row in report.get("properties", [])[:5]
                        ],
                    },
                )
        elif prefix == "p2" and operation == "add_property":
            _start_property_form(conn, update, user_id, actions)
        elif prefix == "p2" and operation == "list_properties":
            _property_list(
                update,
                actions,
                intelligence.search_properties(conn, available_only=False, limit=20),
                "Property inventory",
            )
        elif prefix == "p2" and operation in {"search_properties", "availability_search"}:
            _state(conn, user_id, "P2_PROPERTY_SEARCH", payload={"purpose": operation})
            _edit(
                update,
                actions,
                "Send a title, project, or location to search.",
                InlineKeyboardMarkup([[button("Cancel", "p2:cancel")]]),
            )
        elif prefix == "p2" and operation == "property_performance":
            prefs = crm.ensure_user(conn, user_id, settings)
            _sales_report_screen(conn, update, actions, "last30", prefs["timezone"], settings)
        elif prefix == "p2" and operation == "requirements" and len(parts) == 3:
            pending = conn.execute(
                "select purpose from user_input_state where telegram_user_id=%s", (user_id,)
            ).fetchone()
            if pending and pending.get("purpose") == "P2_PROFILE_INPUT":
                _clear(conn, user_id)
            _show_profile(conn, update, actions, UUID(parts[2]))
        elif prefix == "p2" and operation == "profile_complete" and len(parts) == 3:
            contact_id = UUID(parts[2])
            values = _profile_values(_property_profile(conn, contact_id))
            values["profile_complete"] = True
            _save_profile(conn, contact_id, values, settings, user_id)
            _show_profile(conn, update, actions, contact_id)
        elif prefix == "p2" and operation == "pe" and len(parts) == 4:
            contact_id, code = UUID(parts[2]), parts[3]
            field = next((name for name, short in PROFILE_CODES.items() if short == code), None)
            if not field:
                raise ValueError("Unknown buyer profile field")
            choices = {
                "property_type": PROPERTY_TYPES + [("Unknown", "__UNKNOWN__")],
                "purchase_purpose": [(name, val) for name, val in PURPOSES],
                "preferred_payment_plan": [(name, val) for name, val in PAYMENTS],
                "expected_timeline": [(name, val) for name, val in TIMELINES],
                "new_or_resale": [
                    ("New", "NEW"),
                    ("Resale", "RESALE"),
                    ("Either", "EITHER"),
                    ("Unknown", "UNKNOWN"),
                ],
            }
            if field in choices:
                rows = [
                    [
                        button(
                            label,
                            f"p2:pc:{contact_id}:{code}:{PROFILE_VALUE_CODES.get(value, value.lower())}",
                        )
                    ]
                    for label, value in choices[field]
                ]
                rows.append([button("Cancel", f"p2:requirements:{contact_id}")])
                _edit(
                    update,
                    actions,
                    f"Choose {PROFILE_FIELDS[field].lower()}.",
                    InlineKeyboardMarkup(rows),
                )
            else:
                _state(conn, user_id, "P2_PROFILE_INPUT", contact_id, {"field": field})
                prompt = {
                    "preferred_location": "Type a location or `-` if unknown.",
                    "accepted_locations": "Type acceptable alternative locations separated by commas, or `-`.",
                    "budget": "Type minimum maximum currency, e.g. 500000 1500000 ETB. Use `-` if unknown.",
                    "bedrooms": "Type minimum and maximum bedrooms, e.g. 1 3. Use `-` if unknown.",
                    "size": "Type minimum and maximum square metres, e.g. 60 100. Use `-` if unknown.",
                    "floor_preference": "Type a floor preference or `-` if unknown.",
                    "additional_requirements": "Type additional requirements or `-` if none.",
                }.get(field, "Enter a value or `-` if unknown.")
                _edit(
                    update,
                    actions,
                    prompt,
                    InlineKeyboardMarkup([[button("Cancel", f"p2:requirements:{contact_id}")]]),
                    "Markdown",
                )
        elif prefix == "p2" and operation == "pc" and len(parts) == 5:
            contact_id, code, token = UUID(parts[2]), parts[3], parts[4]
            field = next((name for name, short in PROFILE_CODES.items() if short == code), None)
            if not field:
                raise ValueError("Unknown buyer profile field")
            value = next(
                (canonical for canonical, short in PROFILE_VALUE_CODES.items() if short == token),
                token.upper(),
            )
            values = _profile_values(_property_profile(conn, contact_id))
            if field == "property_type" and value == "__UNKNOWN__":
                values[field] = None
            else:
                values[field] = value
            _save_profile(conn, contact_id, values, settings, user_id)
            _show_profile(conn, update, actions, contact_id)
        elif prefix == "p2" and operation == "qualify" and len(parts) == 3:
            _qualification_screen(conn, update, user_id, actions, UUID(parts[2]))
        elif prefix == "p2" and operation == "qs" and len(parts) == 5:
            contact_id, code, value = UUID(parts[2]), parts[3], parts[4]
            if value not in {"0", "1"}:
                raise ValueError("Unknown qualification value")
            field = next(
                (name for name, short in QUALIFICATION_CODES.items() if short == code), None
            )
            if not field:
                raise ValueError("Unknown qualification field")
            values = {field: value == "1"}
            if field == "budget_disclosed" and value == "1":
                profile = _property_profile(conn, contact_id)
                if profile.get("min_budget") is None and profile.get("max_budget") is None:
                    _edit(
                        update,
                        actions,
                        "Record a budget first, or mark that the buyer declined to disclose it.",
                        InlineKeyboardMarkup(
                            [
                                [button("Property Requirements", f"p2:requirements:{contact_id}")],
                                [button("Back", f"p2:qualify:{contact_id}")],
                            ]
                        ),
                    )
                    return True
                values["budget_declined"] = False
            if field == "budget_declined" and value == "1":
                values["budget_disclosed"] = False
            intelligence.save_qualification(
                conn,
                contact_id,
                user_id,
                values,
                confirm_status=False,
                score_weights=intelligence.lead_weights_from_settings(settings),
            )
            _qualification_screen(conn, update, user_id, actions, contact_id)
        elif prefix == "p2" and operation == "qaction" and len(parts) == 3:
            contact_id = UUID(parts[2])
            _state(conn, user_id, "P2_QACTION", contact_id)
            _edit(
                update,
                actions,
                "Enter the concrete next action, such as `call Saturday at 10:00`. Send `-` to clear.",
                InlineKeyboardMarkup([[button("Cancel", f"p2:qualify:{contact_id}")]]),
                "Markdown",
            )
        elif prefix == "p2" and operation == "qreview" and len(parts) == 3:
            _qualification_review(conn, update, actions, UUID(parts[2]))
        elif prefix == "p2" and operation == "qconfirm" and len(parts) == 3:
            contact_id = UUID(parts[2])
            result = intelligence.save_qualification(
                conn,
                contact_id,
                user_id,
                {},
                confirm_status=True,
                score_weights=intelligence.lead_weights_from_settings(settings),
            )
            _edit(
                update,
                actions,
                f"Qualification status saved: {result['status'].replace('_', ' ').title()}.",
                InlineKeyboardMarkup(
                    [
                        [button("Back to Checklist", f"p2:qualify:{contact_id}")],
                        [button("Contact", f"lead:{contact_id}")],
                    ]
                ),
            )
        elif prefix == "p2" and operation == "qdisqualify" and len(parts) == 3:
            contact_id = UUID(parts[2])
            _edit(
                update,
                actions,
                "Confirm this buyer is disqualified. This does not change the raw call outcome.",
                InlineKeyboardMarkup(
                    [
                        [button("Confirm Disqualified", f"p2:qdq:{contact_id}")],
                        [button("Cancel", f"p2:qualify:{contact_id}")],
                    ]
                ),
            )
        elif prefix == "p2" and operation == "qdq" and len(parts) == 3:
            result = intelligence.save_qualification(
                conn,
                UUID(parts[2]),
                user_id,
                {},
                manual_status="DISQUALIFIED",
                score_weights=intelligence.lead_weights_from_settings(settings),
            )
            _edit(
                update,
                actions,
                "Buyer marked disqualified.",
                InlineKeyboardMarkup([[button("Back to Contact", f"lead:{parts[2]}")]]),
            )
        elif prefix == "p2" and operation == "pv" and len(parts) == 3:
            state = conn.execute(
                "select * from user_input_state where telegram_user_id=%s and purpose='P2_PROPERTY_FORM' and expires_at>now()",
                (user_id,),
            ).fetchone()
            if not state:
                raise ValueError("Property form expired")
            payload = state["payload"] or {}
            index = int(payload.get("step", 0))
            field = PROPERTY_FORM_FIELDS[index][0]
            if field not in {
                "property_type",
                "payment_plans_verified",
                "construction_status",
                "availability_status",
            }:
                raise ValueError("Property form changed")
            value: Any = parts[2]
            if field == "payment_plans_verified":
                value = value == "true"
            payload.setdefault("values", {})[field] = value
            payload["step"] = index + 1
            _state(conn, user_id, "P2_PROPERTY_FORM", payload=payload)
            _property_form_prompt(conn, update, user_id, actions, payload)
        elif prefix == "p2" and operation == "property_save":
            state = conn.execute(
                "select * from user_input_state where telegram_user_id=%s and purpose='P2_PROPERTY_CONFIRM' and expires_at>now()",
                (user_id,),
            ).fetchone()
            if not state:
                raise ValueError("Property form expired")
            payload = state["payload"] or {}
            values = PropertyInput.model_validate(payload.get("values") or {}).model_dump(
                mode="python"
            )
            prop_id = UUID(payload["property_id"]) if payload.get("property_id") else None
            row = intelligence.save_property(conn, values, prop_id)
            conn.execute(
                "insert into audit_events (telegram_user_id,property_id,event_type,new_value) values (%s,%s,%s,%s)",
                (
                    user_id,
                    row["id"],
                    "PROPERTY_UPDATED" if prop_id else "PROPERTY_CREATED",
                    json.dumps(
                        {"title": row["title"], "availability_status": row["availability_status"]}
                    ),
                ),
            )
            _clear(conn, user_id)
            _edit(
                update,
                actions,
                "Property record saved.",
                InlineKeyboardMarkup(
                    [
                        [button("View Property", f"p2:prop_detail:{row['id']}")],
                        [button("Property Inventory", "m:properties")],
                    ]
                ),
            )
        elif prefix == "p2" and operation.startswith("prop_") and len(parts) == 3:
            action = operation[5:]
            property_id = UUID(parts[2])
            if action == "detail":
                pending = conn.execute(
                    "select purpose from user_input_state where telegram_user_id=%s", (user_id,)
                ).fetchone()
                if pending and pending.get("purpose") in {
                    "P2_PROPERTY_PRICE",
                    "P2_PRICE_CONFIRM",
                }:
                    _clear(conn, user_id)
                context = conn.execute(
                    "select * from user_input_state where telegram_user_id=%s and purpose='P2_BUYER_CONTEXT' and expires_at>now()",
                    (user_id,),
                ).fetchone()
                contact_id = context.get("contact_id") if context else None
                _property_detail(conn, update, actions, property_id, contact_id)
            elif action == "availability":
                raise ValueError("Use availability action from property detail")
            elif action == "edit":
                existing = intelligence.get_property(conn, property_id)
                if not existing:
                    raise ValueError("Property no longer exists")
                _start_property_form(conn, update, user_id, actions, existing)
        elif prefix == "p2" and operation == "edit_property" and len(parts) == 3:
            existing = intelligence.get_property(conn, UUID(parts[2]))
            if not existing:
                raise ValueError("Property no longer exists")
            _start_property_form(conn, update, user_id, actions, existing)
        elif prefix == "p2" and operation == "availability" and len(parts) == 3:
            property_id = UUID(parts[2])
            choices = [
                ("Available", "AVAILABLE"),
                ("Reserved", "RESERVED"),
                ("Sold", "SOLD"),
                ("Temporarily unavailable", "TEMPORARILY_UNAVAILABLE"),
                ("Unverified", "UNVERIFIED"),
            ]
            short_status = {
                "AVAILABLE": "A",
                "RESERVED": "R",
                "SOLD": "S",
                "TEMPORARILY_UNAVAILABLE": "T",
                "UNVERIFIED": "U",
            }
            _edit(
                update,
                actions,
                "Set verified availability status.",
                InlineKeyboardMarkup(
                    [
                        [button(label, f"p2:av:{property_id}:{short_status[status]}")]
                        for label, status in choices
                    ]
                    + [[button("Cancel", f"p2:prop_detail:{property_id}")]]
                ),
            )
        elif prefix == "p2" and operation == "av" and len(parts) == 4:
            status = {
                "A": "AVAILABLE",
                "R": "RESERVED",
                "S": "SOLD",
                "T": "TEMPORARILY_UNAVAILABLE",
                "U": "UNVERIFIED",
            }.get(parts[3])
            if not status:
                raise ValueError("Unsupported availability")
            prop = intelligence.update_availability(conn, UUID(parts[2]), status, user_id)
            _edit(
                update,
                actions,
                f"Availability updated to {prop['availability_status']}.",
                InlineKeyboardMarkup([[button("View Property", f"p2:prop_detail:{prop['id']}")]]),
            )
        elif prefix == "p2" and operation == "edit_price" and len(parts) == 3:
            property_id = UUID(parts[2])
            _state(conn, user_id, "P2_PROPERTY_PRICE", payload={"property_id": str(property_id)})
            _edit(
                update,
                actions,
                "Enter a non-negative price, or `-` if unverified.",
                InlineKeyboardMarkup([[button("Cancel", f"p2:prop_detail:{property_id}")]]),
                "Markdown",
            )
        elif prefix == "p2" and operation == "price_confirm":
            state = conn.execute(
                "select * from user_input_state where telegram_user_id=%s and purpose='P2_PRICE_CONFIRM' and expires_at>now()",
                (user_id,),
            ).fetchone()
            if not state:
                raise ValueError("Price update expired")
            payload = state["payload"] or {}
            prop = intelligence.get_property(conn, UUID(payload["property_id"]))
            conn.execute(
                "update properties set price=%s,updated_at=now() where id=%s",
                (payload.get("price"), prop["id"]),
            )
            conn.execute(
                "insert into audit_events (telegram_user_id,property_id,event_type,old_value,new_value) values (%s,%s,'PROPERTY_PRICE_UPDATED',%s,%s)",
                (
                    user_id,
                    prop["id"],
                    json.dumps({"price": prop.get("price")}),
                    json.dumps({"price": payload.get("price")}),
                ),
            )
            _clear(conn, user_id)
            _edit(
                update,
                actions,
                "Property price updated.",
                InlineKeyboardMarkup([[button("View Property", f"p2:prop_detail:{prop['id']}")]]),
            )
        elif prefix == "p2" and operation == "property_matches" and len(parts) == 3:
            _property_buyer_matches(conn, update, user_id, actions, UUID(parts[2]), settings)
        elif prefix == "p2" and operation == "buyer_reject" and len(parts) == 3:
            state = conn.execute(
                "select * from user_input_state where telegram_user_id=%s "
                "and purpose='P2_MATCH_CONTEXT' and expires_at>now()",
                (user_id,),
            ).fetchone()
            contact_id = UUID(parts[2])
            records = (state.get("payload") or {}).get("recommendations", {}) if state else {}
            recommendation_id = records.get(str(contact_id))
            if not _sales_actions_allowed(crm.get_contact(conn, contact_id)):
                _edit(update, actions, "This buyer is no longer eligible for recommendations.")
            elif not recommendation_id:
                _edit(update, actions, "The recommendation record is no longer available.")
            else:
                _state(
                    conn,
                    user_id,
                    "P2_RECOMMENDATION_REJECT",
                    contact_id,
                    {
                        "recommendation_id": str(recommendation_id),
                        "property_id": str((state.get("payload") or {}).get("property_id")),
                    },
                )
                _edit(
                    update,
                    actions,
                    "Why is this property a poor fit? Send a short reason.",
                    InlineKeyboardMarkup([[button("Cancel", "p2:cancel")]]),
                )
        elif prefix == "p2" and operation == "buyer_interest" and len(parts) == 3:
            state = conn.execute(
                "select * from user_input_state where telegram_user_id=%s and purpose='P2_MATCH_CONTEXT' and expires_at>now()",
                (user_id,),
            ).fetchone()
            payload = state.get("payload") if state else None
            contact_id = UUID(parts[2])
            rec_id = (payload or {}).get("recommendations", {}).get(str(contact_id))
            if not _sales_actions_allowed(crm.get_contact(conn, contact_id)):
                _edit(update, actions, "This buyer is no longer eligible for recommendations.")
            elif rec_id:
                intelligence.update_recommendation(
                    conn, UUID(rec_id), "INTERESTED", "Buyer interest recorded by agent"
                )
                _edit(
                    update,
                    actions,
                    "Buyer interest recorded.",
                    InlineKeyboardMarkup([[button("View Buyer", f"lead:{contact_id}")]]),
                )
            else:
                _edit(update, actions, "Recommendation record is no longer available.")
        elif prefix == "p2" and operation == "buyer_matches" and len(parts) == 3:
            _buyer_property_matches(conn, update, user_id, actions, UUID(parts[2]), settings)
        elif prefix == "p2" and operation == "bprop" and len(parts) == 3:
            state = conn.execute(
                "select * from user_input_state where telegram_user_id=%s and purpose='P2_BUYER_CONTEXT' and expires_at>now()",
                (user_id,),
            ).fetchone()
            if not state or not state.get("contact_id"):
                _edit(
                    update,
                    actions,
                    "Buyer match context expired. Open the buyer's match list again.",
                )
            else:
                _property_detail(conn, update, actions, UUID(parts[2]), state["contact_id"])
        elif prefix == "p2" and operation == "reject_match" and len(parts) == 3:
            state = conn.execute(
                "select * from user_input_state where telegram_user_id=%s "
                "and purpose='P2_BUYER_CONTEXT' and expires_at>now()",
                (user_id,),
            ).fetchone()
            if not state or not state.get("contact_id"):
                raise ValueError("Buyer match context expired")
            contact_id, property_id = state["contact_id"], UUID(parts[2])
            if not _sales_actions_allowed(crm.get_contact(conn, contact_id)):
                _edit(update, actions, "This buyer is no longer eligible for recommendations.")
            else:
                prop = intelligence.get_property(conn, property_id)
                if not prop:
                    raise ValueError("Property no longer exists")
                match = intelligence.match_property(
                    _property_profile(conn, contact_id),
                    prop,
                    weights=intelligence.match_weights_from_settings(settings),
                    allow_near_budget=getattr(settings, "match_allow_near_budget", False),
                )
                rec = intelligence.save_recommendation(conn, contact_id, property_id, match)
                _state(
                    conn,
                    user_id,
                    "P2_RECOMMENDATION_REJECT",
                    contact_id,
                    {
                        "recommendation_id": str(rec["id"]),
                        "property_id": str(property_id),
                    },
                )
                _edit(
                    update,
                    actions,
                    "Why is this property a poor fit? Send a short reason.",
                    InlineKeyboardMarkup([[button("Cancel", "p2:cancel")]]),
                )
        elif prefix == "p2" and operation in {"recommend", "interest", "visit"} and len(parts) == 3:
            state = conn.execute(
                "select * from user_input_state where telegram_user_id=%s and purpose='P2_BUYER_CONTEXT' and expires_at>now()",
                (user_id,),
            ).fetchone()
            if not state or not state.get("contact_id"):
                raise ValueError("Buyer match context expired")
            contact_id, property_id = state["contact_id"], UUID(parts[2])
            prop = intelligence.get_property(conn, property_id)
            if not _sales_actions_allowed(crm.get_contact(conn, contact_id)):
                _edit(update, actions, "This buyer is no longer eligible for recommendations.")
            elif not prop or prop["availability_status"] != "AVAILABLE":
                _edit(update, actions, "This property is not available for a new recommendation.")
            elif operation == "visit":
                _state(conn, user_id, "P2_VISIT", contact_id, {"property_id": str(property_id)})
                prefs = crm.ensure_user(conn, user_id, settings)
                _edit(
                    update,
                    actions,
                    f"Enter the exact property visit date and time as YYYY-MM-DD HH:MM in {prefs['timezone']}.",
                    InlineKeyboardMarkup([[button("Cancel", "p2:cancel")]]),
                )
            else:
                profile = _property_profile(conn, contact_id)
                match = intelligence.match_property(
                    profile,
                    prop,
                    weights=intelligence.match_weights_from_settings(settings),
                    allow_near_budget=getattr(settings, "match_allow_near_budget", False),
                )
                if match["rejected"]:
                    _edit(
                        update,
                        actions,
                        "This property no longer passes the buyer's recorded constraints.",
                    )
                else:
                    rec = intelligence.save_recommendation(conn, contact_id, property_id, match)
                    if operation == "interest":
                        intelligence.update_recommendation(
                            conn, rec["id"], "INTERESTED", "Buyer interested after recommendation"
                        )
                    _edit(
                        update,
                        actions,
                        "Buyer interest recorded."
                        if operation == "interest"
                        else "Property recommendation recorded.",
                        InlineKeyboardMarkup(
                            [
                                [button("Back to Buyer", f"lead:{contact_id}")],
                                [button("Property Details", f"p2:bprop:{property_id}")],
                            ]
                        ),
                    )
        elif prefix == "p2" and operation == "script" and len(parts) == 3:
            if not getattr(settings, "ai_enabled", False):
                _edit(update, actions, "AI is disabled.")
            else:
                _script_choices(update, actions, UUID(parts[2]))
        elif prefix == "p2" and operation == "sc" and len(parts) == 5:
            _generate_contact_script(
                conn,
                update,
                user_id,
                settings,
                actions,
                UUID(parts[2]),
                SCRIPT_TYPES[parts[3]],
                parts[4],
            )
        elif prefix == "p2" and operation == "objection" and len(parts) == 3:
            contact_id = UUID(parts[2])
            if not getattr(settings, "ai_enabled", False):
                _edit(update, actions, "AI is disabled.")
            else:
                rows = [
                    [button(label, f"p2:obj:{contact_id}:{i}")]
                    for i, (label, _) in enumerate(OBJECTIONS)
                ]
                rows.append([button("Cancel", f"lead:{contact_id}")])
                _edit(
                    update, actions, "Choose the prospect's objection.", InlineKeyboardMarkup(rows)
                )
        elif prefix == "p2" and operation == "obj" and len(parts) == 4:
            contact_id, index = UUID(parts[2]), int(parts[3])
            if not 0 <= index < len(OBJECTIONS):
                raise ValueError("Unknown objection choice")
            context, error = _ai_context(conn, contact_id)
            if error:
                _edit(update, actions, error)
            else:
                _queue_ai(
                    conn,
                    update,
                    user_id,
                    settings,
                    actions,
                    "OBJECTION",
                    {"objection": OBJECTIONS[index][1], "context": context},
                    contact_id,
                )
        elif prefix == "p2" and operation == "draft" and len(parts) == 3:
            _create_ai_draft(conn, update, user_id, settings, actions, UUID(parts[2]))
        elif prefix == "p2" and operation == "notes" and len(parts) == 3:
            contact_id = UUID(parts[2])
            _, error = _ai_context(conn, contact_id)
            if error:
                _edit(update, actions, error)
            else:
                _state(conn, user_id, "P2_AI_NOTES", contact_id)
                _edit(
                    update,
                    actions,
                    "Send the rough call notes to structure. The original note is retained; nothing is saved from AI without your confirmation.",
                    InlineKeyboardMarkup([[button("Cancel", "p2:cancel")]]),
                )
        elif prefix == "p2" and operation == "sc_contact" and len(parts) == 3:
            _start_contact_lookup(conn, update, user_id, actions, "script")
        elif prefix == "p2" and operation == "select_contact" and len(parts) == 4:
            operation_name, contact_id = parts[2], UUID(parts[3])
            if operation_name == "findprops":
                _buyer_property_matches(conn, update, user_id, actions, contact_id, settings)
            elif operation_name == "qualify":
                _qualification_screen(conn, update, user_id, actions, contact_id)
            elif operation_name == "script":
                _script_choices(update, actions, contact_id)
            elif operation_name == "draft":
                _create_ai_draft(conn, update, user_id, settings, actions, contact_id)
            elif operation_name == "objection":
                rows = [
                    [button(label, f"p2:obj:{contact_id}:{i}")]
                    for i, (label, _) in enumerate(OBJECTIONS)
                ]
                _edit(
                    update, actions, "Choose the prospect's objection.", InlineKeyboardMarkup(rows)
                )
            elif operation_name == "notes":
                _, error = _ai_context(conn, contact_id)
                if error:
                    _edit(update, actions, error)
                else:
                    _state(conn, user_id, "P2_AI_NOTES", contact_id)
                    _edit(
                        update,
                        actions,
                        "Send the rough call notes to structure. They will require your confirmation before being saved.",
                        InlineKeyboardMarkup([[button("Cancel", "p2:cancel")]]),
                    )
        elif prefix == "ai" and operation == "close" and len(parts) == 3:
            _clear(conn, user_id)
            _edit(update, actions, "Closed. No CRM changes were made.", main_menu())
        elif prefix == "ai" and operation == "regenerate" and len(parts) == 3:
            job = _ai_job(conn, user_id, UUID(parts[2]))
            if not job:
                _edit(update, actions, "This AI result is no longer available.")
            elif (job.get("result_payload") or {}).get("do_not_send"):
                _edit(
                    update, actions, "This draft is marked do not send and cannot be regenerated."
                )
            elif job.get("contact_id") and (
                not _sales_actions_allowed(crm.get_contact(conn, job["contact_id"]))
            ):
                _edit(update, actions, "The contact is no longer eligible for AI assistance.")
            else:
                _queue_ai(
                    conn,
                    update,
                    user_id,
                    settings,
                    actions,
                    job["job_type"],
                    job.get("input_payload") or {},
                    job.get("contact_id"),
                    job.get("property_id"),
                )
        elif prefix == "ai" and operation == "copy" and len(parts) == 3:
            job = _ai_job(conn, user_id, UUID(parts[2]))
            if (
                job
                and job["job_type"] == "FOLLOWUP_DRAFT"
                and not (job.get("result_payload") or {}).get("do_not_send")
                and _sales_actions_allowed(
                    crm.get_contact(conn, job["contact_id"]) if job.get("contact_id") else None
                )
            ):
                _edit(update, actions, "Copy the message from the plain-text message below.")
                actions.append(
                    _send(
                        query.message.chat_id, (job.get("result_payload") or {}).get("message", "")
                    )
                )
            else:
                _edit(update, actions, "This draft is unavailable for further contact.")
        elif (
            prefix == "ai"
            and operation in {"confirm", "discard", "edit", "save", "sent"}
            and len(parts) == 3
        ):
            job_id = UUID(parts[2])
            job = _ai_job(conn, user_id, job_id)
            if not job:
                _edit(update, actions, "This AI result is no longer available.")
            elif operation == "confirm" and job["job_type"] == "STRUCTURE_NOTES":
                if not _sales_actions_allowed(
                    crm.get_contact(conn, job.get("contact_id")) if job.get("contact_id") else None
                ):
                    _clear(conn, user_id)
                    _edit(
                        update,
                        actions,
                        "This contact is no longer eligible. The AI notes were not saved.",
                    )
                else:
                    _save_structured_notes(conn, user_id, settings, job)
                    _clear(conn, user_id)
                    _edit(
                        update,
                        actions,
                        "Structured notes confirmed and saved. No ambiguous follow-up was scheduled.",
                        InlineKeyboardMarkup(
                            [[button("Back to Contact", f"lead:{job['contact_id']}")]]
                        ),
                    )
            elif operation == "discard":
                conn.execute(
                    """update ai_jobs set status='CANCELLED',input_payload='{}'::jsonb,
                         result_payload='{}'::jsonb,updated_at=now()
                       where id=%s and telegram_user_id=%s""",
                    (job_id, user_id),
                )
                _clear(conn, user_id)
                _edit(update, actions, "AI suggestions discarded. No CRM changes were made.")
            elif operation == "edit" and job["job_type"] == "STRUCTURE_NOTES":
                if not _sales_actions_allowed(
                    crm.get_contact(conn, job.get("contact_id")) if job.get("contact_id") else None
                ):
                    _edit(update, actions, "This contact is no longer eligible for AI assistance.")
                else:
                    _state(
                        conn,
                        user_id,
                        "P2_AI_NOTES",
                        job.get("contact_id"),
                        {"edit_job_id": str(job_id)},
                    )
                    _edit(
                        update,
                        actions,
                        "Send corrected rough notes. A new structured result will be generated for review.",
                        InlineKeyboardMarkup([[button("Cancel", f"ai:close:{job_id}")]]),
                    )
            elif operation == "edit" and job["job_type"] == "FOLLOWUP_DRAFT":
                if (job.get("result_payload") or {}).get(
                    "do_not_send"
                ) or not _sales_actions_allowed(
                    crm.get_contact(conn, job.get("contact_id")) if job.get("contact_id") else None
                ):
                    _edit(update, actions, "This draft is unavailable for further contact.")
                else:
                    _state(
                        conn,
                        user_id,
                        "P2_AI_DRAFT_EDIT",
                        job.get("contact_id"),
                        {"job_id": str(job_id)},
                    )
                    _edit(
                        update,
                        actions,
                        "Send the edited follow-up message.",
                        InlineKeyboardMarkup([[button("Cancel", f"ai:close:{job_id}")]]),
                    )
            elif operation in {"save", "sent"} and job["job_type"] == "FOLLOWUP_DRAFT":
                contact = (
                    crm.get_contact(conn, job["contact_id"]) if job.get("contact_id") else None
                )
                result = job.get("result_payload") or {}
                if not _sales_actions_allowed(contact):
                    _edit(
                        update,
                        actions,
                        "This contact is not eligible for further messaging. No note was saved.",
                    )
                elif operation == "sent" and result.get("do_not_send"):
                    _edit(
                        update,
                        actions,
                        "This draft is flagged do not send. It cannot be marked sent.",
                    )
                else:
                    crm.save_note(
                        conn,
                        job["contact_id"],
                        result.get("message", ""),
                        "AI_MESSAGE_SENT" if operation == "sent" else "AI_MESSAGE_DRAFT",
                    )
                    conn.execute(
                        "insert into audit_events (telegram_user_id,contact_id,event_type,new_value) values (%s,%s,%s,%s)",
                        (
                            user_id,
                            job["contact_id"],
                            "AI_MESSAGE_SENT" if operation == "sent" else "AI_MESSAGE_SAVED",
                            json.dumps({"job_id": str(job_id)}),
                        ),
                    )
                    _edit(
                        update,
                        actions,
                        "Marked as sent."
                        if operation == "sent"
                        else "Draft saved as a contact note.",
                    )
        else:
            return False
    except (ValueError, TypeError, KeyError, IndexError, ValidationError) as exc:
        logger.info("Rejected Phase 2 callback (%s)", type(exc).__name__)
        _edit(
            update,
            actions,
            "That action is no longer valid. Open the relevant menu and try again.",
            main_menu(),
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
) -> bool:
    message = update.effective_message
    if not message or not message.text:
        return False
    text = message.text.strip()
    purpose = state.get("purpose")
    payload = _state_data(state)
    contact_id = state.get("contact_id")
    try:
        if purpose == "P2_CONTACT_SEARCH":
            _contact_matches(
                conn,
                update,
                user_id,
                settings,
                actions,
                text,
                payload.get("operation", "findprops"),
            )
        elif purpose == "P2_PROFILE_INPUT" and contact_id:
            profile = _property_profile(conn, contact_id)
            values = _profile_values(profile)
            field = payload.get("field")
            if field == "budget":
                if text == "-":
                    values.update(min_budget=None, max_budget=None)
                else:
                    parts = text.replace(",", "").split()
                    if len(parts) != 3:
                        raise ValueError(
                            "Use minimum maximum currency, for example 500000 1500000 ETB"
                        )
                    low = None if parts[0] == "-" else Decimal(parts[0])
                    high = None if parts[1] == "-" else Decimal(parts[1])
                    currency = parts[2].upper()
                    values.update(min_budget=low, max_budget=high, currency=currency)
            elif field == "bedrooms":
                if text == "-":
                    values.update(bedrooms=None, min_bedrooms=None, max_bedrooms=None)
                else:
                    parts = text.split()
                    if len(parts) != 2:
                        raise ValueError("Use minimum and maximum bedroom counts, such as 1 3")
                    low, high = int(parts[0]), int(parts[1])
                    values.update(
                        bedrooms=low if low == high else None, min_bedrooms=low, max_bedrooms=high
                    )
            elif field == "size":
                if text == "-":
                    values.update(min_area_sqm=None, max_area_sqm=None)
                else:
                    parts = text.replace(",", "").split()
                    if len(parts) != 2:
                        raise ValueError(
                            "Use minimum and maximum size in square metres, such as 60 100"
                        )
                    values.update(min_area_sqm=Decimal(parts[0]), max_area_sqm=Decimal(parts[1]))
            elif field == "accepted_locations":
                values[field] = (
                    []
                    if text == "-"
                    else [item.strip() for item in text.split(",") if item.strip()]
                )
            else:
                values[field] = None if text == "-" else text[:2000]
            _save_profile(conn, contact_id, values, settings, user_id)
            _clear(conn, user_id)
            _show_profile(conn, update, actions, contact_id)
        elif purpose == "P2_PROPERTY_FORM":
            index = int(payload.get("step", 0))
            field = PROPERTY_FORM_FIELDS[index][0]
            values = payload.setdefault("values", {})
            if field == "title" and text == "-":
                _clear(conn, user_id)
                _inventory_menu(update, actions)
                return True
            if field in {
                "property_type",
                "payment_plans_verified",
                "construction_status",
                "availability_status",
            }:
                raise ValueError("Choose one of the inline options shown above")
            value: Any = None if text == "-" else text
            if field in {"title", "currency"} and text == "-":
                raise ValueError(f"{PROPERTY_FORM_FIELDS[index][1]} is required")
            if field in {"price", "minimum_negotiable_price", "area_sqm"} and value is not None:
                try:
                    value = Decimal(text.replace(",", ""))
                    if value < 0:
                        raise ValueError
                except (InvalidOperation, ValueError) as exc:
                    raise ValueError("Enter a non-negative number, or `-` if unknown") from exc
            elif field in {"bedrooms", "bathrooms"} and value is not None:
                try:
                    value = int(text)
                    if value < 0:
                        raise ValueError
                except ValueError as exc:
                    raise ValueError(
                        "Enter a non-negative whole number, or `-` if unknown"
                    ) from exc
            elif field == "currency" and value is not None:
                value = text.upper()
                if len(value) != 3 or not value.isalpha():
                    raise ValueError("Enter a three-letter currency code, such as ETB or USD")
            elif field == "delivery_date" and value is not None:
                try:
                    date.fromisoformat(text)
                except ValueError as exc:
                    raise ValueError("Use YYYY-MM-DD, or `-` if unknown") from exc
            elif field == "available_payment_plans" and value is not None:
                value = [item.strip().upper() for item in text.split(",") if item.strip()]
            values[field] = value
            payload["step"] = index + 1
            _state(conn, user_id, "P2_PROPERTY_FORM", payload=payload)
            _property_form_prompt(conn, update, user_id, actions, payload)
        elif purpose == "P2_PROPERTY_SEARCH":
            _clear(conn, user_id)
            available_only = payload.get("purpose") != "availability_search"
            rows = intelligence.search_properties(conn, text, available_only, 20)
            _property_list(update, actions, rows, "Search results")
        elif purpose == "P2_PROPERTY_PRICE":
            property_id = UUID(payload["property_id"])
            prop = intelligence.get_property(conn, property_id)
            if not prop:
                raise ValueError("Property no longer exists")
            if text == "-":
                price = None
                value_label = "Unknown"
            else:
                try:
                    price = Decimal(text.replace(",", ""))
                except InvalidOperation as exc:
                    raise ValueError(
                        "Enter a non-negative numeric price, or `-` if unknown"
                    ) from exc
                if price < 0:
                    raise ValueError("Price cannot be negative")
                price = PropertyInput(
                    title=prop["title"],
                    property_type=prop["property_type"],
                    currency=prop["currency"],
                    price=price,
                    minimum_negotiable_price=prop.get("minimum_negotiable_price"),
                ).price
                if (
                    prop.get("minimum_negotiable_price") is not None
                    and price < prop["minimum_negotiable_price"]
                ):
                    raise ValueError("Listed price cannot be below the minimum negotiable price")
                value_label = f"{price} {prop['currency']}"
            _state(
                conn,
                user_id,
                "P2_PRICE_CONFIRM",
                payload={
                    "property_id": str(property_id),
                    "price": str(price) if price is not None else None,
                },
            )
            _edit(
                update,
                actions,
                f"Confirm the property price update to {value_label}.",
                InlineKeyboardMarkup(
                    [
                        [button("Confirm Price", "p2:price_confirm")],
                        [button("Cancel", f"p2:prop_detail:{property_id}")],
                    ]
                ),
            )
        elif purpose == "P2_QACTION" and contact_id:
            intelligence.save_qualification(
                conn,
                contact_id,
                user_id,
                {"next_action": None if text == "-" else text[:300]},
                confirm_status=False,
                score_weights=intelligence.lead_weights_from_settings(settings),
            )
            _clear(conn, user_id)
            _qualification_screen(conn, update, user_id, actions, contact_id)
        elif purpose == "P2_VISIT" and contact_id:
            if not _sales_actions_allowed(crm.get_contact(conn, contact_id)):
                raise ValueError(
                    "This buyer is no longer eligible for a property visit recommendation"
                )
            prefs = crm.ensure_user(conn, user_id, settings)
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(
                    tzinfo=ZoneInfo(prefs["timezone"])
                )
            except ValueError as exc:
                raise ValueError("Use YYYY-MM-DD HH:MM in your configured timezone") from exc
            scheduled = parsed.astimezone(ZoneInfo("UTC"))
            if scheduled <= datetime.now(ZoneInfo("UTC")):
                raise ValueError("Choose a future date and time")
            property_id = UUID(payload["property_id"])
            prop = intelligence.get_property(conn, property_id)
            if not prop or prop["availability_status"] != "AVAILABLE":
                raise ValueError("This property is no longer marked available")
            profile = _property_profile(conn, contact_id)
            match = intelligence.match_property(
                profile,
                prop,
                weights=intelligence.match_weights_from_settings(settings),
                allow_near_budget=getattr(settings, "match_allow_near_budget", False),
            )
            if match["rejected"]:
                raise ValueError("This property no longer passes the buyer's recorded constraints")
            rec = intelligence.save_recommendation(conn, contact_id, property_id, match)
            intelligence.update_recommendation(
                conn, rec["id"], "VISIT_SCHEDULED", "Property visit scheduled by agent"
            )
            intelligence.save_qualification(
                conn,
                contact_id,
                user_id,
                {
                    "requested_property_visit": True,
                    "next_action": (
                        f"Property visit for {prop['title']} on "
                        f"{scheduled.strftime('%Y-%m-%d %H:%M')} {prefs['timezone']}"
                    ),
                },
                confirm_status=False,
                score_weights=intelligence.lead_weights_from_settings(settings),
            )
            crm.schedule_followup(
                conn,
                contact_id,
                None,
                scheduled,
                "PROPERTY_VISIT",
                f"Visit {prop['title']}",
                telegram_user_id=user_id,
            )
            _clear(conn, user_id)
            _edit(
                update,
                actions,
                f"Property visit scheduled for {scheduled.strftime('%Y-%m-%d %H:%M')} ({prefs['timezone']}).",
                InlineKeyboardMarkup(
                    [
                        [button("View Buyer", f"lead:{contact_id}")],
                        [button("View Property", f"p2:prop_detail:{property_id}")],
                    ]
                ),
            )
        elif purpose == "P2_RECOMMENDATION_REJECT" and contact_id:
            reason = text.strip()[:500]
            if not reason or reason == "-":
                raise ValueError("Enter a short rejection reason, or choose Cancel")
            recommendation_id = UUID(payload["recommendation_id"])
            if not intelligence.update_recommendation(
                conn, recommendation_id, "REJECTED", "Agent rejected match", reason
            ):
                raise ValueError("The recommendation is no longer available")
            property_id = UUID(payload["property_id"]) if payload.get("property_id") else None
            conn.execute(
                """insert into audit_events (telegram_user_id,contact_id,property_id,event_type,new_value)
                   values (%s,%s,%s,'PROPERTY_RECOMMENDATION_REJECTED',%s)""",
                (user_id, contact_id, property_id, json.dumps({"reason": reason})),
            )
            _clear(conn, user_id)
            _edit(
                update,
                actions,
                "Recommendation rejection saved with the reason.",
                InlineKeyboardMarkup([[button("Back to Buyer", f"lead:{contact_id}")]]),
            )
        elif purpose == "P2_AI_NOTES" and contact_id:
            if not getattr(settings, "ai_enabled", False):
                _clear(conn, user_id)
                _edit(update, actions, "AI is disabled. Your rough notes were not saved.")
            elif not _sales_actions_allowed(contact := crm.get_contact(conn, contact_id)):
                _clear(conn, user_id)
                _edit(
                    update,
                    actions,
                    "This contact is no longer eligible. Your rough notes were not sent to the AI provider.",
                )
            elif len(text) > settings.ai_max_input_chars:
                raise ValueError("Notes exceed the configured AI input limit")
            else:
                context = {
                    "notes": _redact_contact_numbers(text, contact)[: settings.ai_max_input_chars],
                    "source_note": text,
                    "existing_preferences": {
                        key: _property_profile(conn, contact_id).get(key)
                        for key in (
                            "property_type",
                            "preferred_location",
                            "min_budget",
                            "max_budget",
                            "currency",
                            "preferred_payment_plan",
                            "expected_timeline",
                        )
                    },
                }
                _clear(conn, user_id)
                _queue_ai(
                    conn, update, user_id, settings, actions, "STRUCTURE_NOTES", context, contact_id
                )
        elif purpose == "P2_AI_DRAFT_EDIT" and contact_id:
            job_id = UUID(payload["job_id"])
            if len(text) > 2000:
                raise ValueError("Follow-up message must be 2,000 characters or fewer")
            job = _ai_job(conn, user_id, job_id)
            if not job or job["job_type"] != "FOLLOWUP_DRAFT":
                raise ValueError("Draft is no longer available")
            result = job.get("result_payload") or {}
            result["message"] = text
            result["do_not_send"] = False
            conn.execute(
                "update ai_jobs set result_payload=%s,updated_at=now() where id=%s",
                (json.dumps(result), job_id),
            )
            _clear(conn, user_id)
            _format_followup_output(update, actions, {**job, "result_payload": result}, job_id)
        elif purpose == "P2_QUEUE_FILTER_INPUT":
            key = payload.get("key")
            crm.ensure_user(conn, user_id, settings)
            if key == "preferred_location":
                crm.save_queue_filters(
                    conn, user_id, {"preferred_location": None if text == "-" else text[:120]}
                )
            elif key == "budget":
                if text == "-":
                    crm.clear_queue_filter(conn, user_id, "budget")
                else:
                    parts = text.replace(",", "").split()
                    if len(parts) != 3:
                        raise ValueError("Use minimum maximum currency, e.g. 500000 3000000 ETB")
                    low = None if parts[0] == "-" else Decimal(parts[0])
                    high = None if parts[1] == "-" else Decimal(parts[1])
                    currency = parts[2].upper()
                    if low is not None and low < 0 or high is not None and high < 0:
                        raise ValueError("Budget range cannot be negative")
                    if low is not None and high is not None and low > high:
                        raise ValueError("Minimum budget cannot exceed maximum budget")
                    if len(currency) != 3 or not currency.isalpha():
                        raise ValueError("Use a three-letter currency code")
                    validated_budget = PropertyInterestInput(
                        min_budget=low, max_budget=high, currency=currency
                    )
                    crm.save_queue_filters(
                        conn,
                        user_id,
                        {
                            "min_budget": str(validated_budget.min_budget)
                            if validated_budget.min_budget is not None
                            else None,
                            "max_budget": str(validated_budget.max_budget)
                            if validated_budget.max_budget is not None
                            else None,
                            "currency": validated_budget.currency,
                        },
                    )
            else:
                raise ValueError("Unsupported queue filter")
            _clear(conn, user_id)
            _queue_filter_screen(conn, update, user_id, settings, actions)
        elif purpose == "P2_CUSTOM_REPORT":
            parts = text.split()
            if len(parts) != 2:
                raise ValueError("Enter two dates: YYYY-MM-DD YYYY-MM-DD")
            start, end = date.fromisoformat(parts[0]), date.fromisoformat(parts[1])
            if start > end:
                raise ValueError("Start date must be on or before end date")
            prefs = crm.ensure_user(conn, user_id, settings)
            _clear(conn, user_id)
            _sales_report_screen(
                conn,
                update,
                actions,
                "custom",
                prefs["timezone"],
                settings,
                custom_start=start,
                custom_end=end,
            )
        else:
            return False
    except (ValueError, TypeError, InvalidOperation, ValidationError) as exc:
        _edit(
            update, actions, str(exc)[:500], InlineKeyboardMarkup([[button("Cancel", "p2:cancel")]])
        )
    return True
