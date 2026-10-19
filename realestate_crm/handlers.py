import html
import json
import logging
import re
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import Connection
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

from realestate_crm import phase2_handlers, phase3_handlers, phase4_handlers
from realestate_crm.config import Settings
from realestate_crm.enums import LeadStatus
from realestate_crm.models import PropertyInterestInput
from realestate_crm.phone import tel_uri
from realestate_crm.services import crm, intelligence
from realestate_crm.services import team as team_service
from realestate_crm.services.text_import import parse_pasted_leads
from realestate_crm.telegram_ui import (
    OUTCOME_CODE_CATEGORIES,
    OUTCOMES,
    button,
    campaign_keyboard,
    dashboard_text,
    expand_callback_data,
    export_date_format_keyboard,
    export_format_keyboard,
    export_keyboard,
    followup_keyboard,
    followups_keyboard,
    format_history,
    lead_keyboard,
    lead_text,
    main_menu,
    outcome_categories,
    outcomes_keyboard,
    performance_keyboard,
    report_text,
    search_keyboard,
)
from realestate_crm.timeutils import local_label

logger = logging.getLogger(__name__)

_CALL_CONFIRMATION_CODES = {
    "d": ("DISQUALIFICATION", "DO_NOT_CONTACT"),
    "n": ("CONNECTION", "NO_ANSWER"),
    "b": ("CONNECTION", "BUSY"),
    "s": ("CONNECTION", "PHONE_SWITCHED_OFF"),
    "u": ("CONNECTION", "UNREACHABLE"),
    "f": ("CONNECTION", "CALL_FAILED"),
}
_CALL_CONFIRMATION_CODES_REVERSED = {
    result: code for code, result in _CALL_CONFIRMATION_CODES.items()
}


def _call_confirmation_callback(contact_id: UUID, category: str, outcome: str) -> str:
    try:
        code = _CALL_CONFIRMATION_CODES_REVERSED[(category, outcome)]
    except KeyError as exc:
        raise ValueError("Unsupported call confirmation") from exc
    return f"cc:{contact_id}:{code}"


def _decode_outcome_callback(category: str, value: str) -> tuple[str, str]:
    """Decode compact outcome buttons while accepting already-issued legacy buttons."""
    if category in OUTCOMES:
        if value in {code for _label, code in OUTCOMES[category]}:
            return category, value
        raise ValueError("Unknown call outcome")

    decoded_category = OUTCOME_CODE_CATEGORIES.get(category)
    if decoded_category is None:
        raise ValueError("Unknown call outcome")
    try:
        return decoded_category, OUTCOMES[decoded_category][int(value)][1]
    except (ValueError, IndexError):
        raise ValueError("Unknown call outcome") from None


def _action_send(
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


def _action_edit(
    message, text: str, markup: InlineKeyboardMarkup | None = None, parse_mode: str | None = None
) -> dict[str, Any]:
    return {
        "type": "edit",
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "text": text,
        "markup": markup,
        "parse_mode": parse_mode,
    }


def _action_answer(callback_id: str, text: str = "") -> dict[str, Any]:
    return {"type": "answer", "callback_id": callback_id, "text": text[:180]}


def _set_input(
    conn: Connection,
    user_id: int,
    purpose: str,
    contact_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """insert into user_input_state (telegram_user_id, purpose, contact_id, payload, expires_at)
           values (%s,%s,%s,%s,now()+interval '30 minutes')
           on conflict (telegram_user_id) do update set purpose=excluded.purpose,
             contact_id=excluded.contact_id,payload=excluded.payload,expires_at=excluded.expires_at,updated_at=now()""",
        (user_id, purpose, contact_id, json.dumps(payload or {})),
    )


def _clear_input(conn: Connection, user_id: int) -> None:
    conn.execute("delete from user_input_state where telegram_user_id=%s", (user_id,))


def _get_input(conn: Connection, user_id: int) -> dict[str, Any] | None:
    return conn.execute(
        "select * from user_input_state where telegram_user_id=%s and expires_at>now()",
        (user_id,),
    ).fetchone()


def _edit_or_send(
    actions: list[dict[str, Any]],
    update: Update,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    message = update.effective_message
    if update.callback_query and message:
        actions.append(_action_edit(message, text, markup, parse_mode))
    elif message:
        actions.append(_action_send(message.chat_id, text, markup, parse_mode))


def _dashboard(
    conn: Connection,
    user_id: int,
    settings: Settings,
    actions: list[dict[str, Any]],
    update: Update,
) -> None:
    data = crm.dashboard(conn, user_id, settings)
    _edit_or_send(
        actions,
        update,
        dashboard_text(data),
        phase4_handlers.role_keyboard(conn, user_id),
        ParseMode.MARKDOWN,
    )


def _show_contact(
    conn: Connection,
    update: Update,
    contact_id: UUID,
    user_id: int,
    prefs: dict[str, Any],
    actions: list[dict[str, Any]],
    session_id: UUID | None = None,
    settings: Settings | None = None,
) -> None:
    if settings:
        intelligence.recalculate_lead_score(
            conn, contact_id, intelligence.lead_weights_from_settings(settings)
        )
    contact = crm.get_contact(conn, contact_id)
    if not contact:
        _edit_or_send(actions, update, "This contact is no longer available.", main_menu())
        return
    if session_id is None:
        session = crm.current_session(conn, user_id)
        session_id = session["id"] if session and session["status"] == "ACTIVE" else None
    _edit_or_send(
        actions,
        update,
        lead_text(contact, prefs["timezone"]),
        lead_keyboard(
            contact,
            str(session_id) if session_id else None,
            bool(getattr(settings, "ai_enabled", False)),
        ),
        ParseMode.HTML,
    )


def _search_results(
    conn: Connection,
    update: Update,
    user_id: int,
    query: str,
    limit: int,
    actions: list[dict[str, Any]],
    mode: str = "name_phone",
    timezone: str = "Africa/Addis_Ababa",
    after_id: UUID | None = None,
    has_previous: bool = False,
) -> None:
    results = crm.search_filtered(conn, mode, query, limit, after_id, timezone)
    rows = [
        [button(f"{row['name'][:32]} · {row['phone_number'][:22]}", f"lead:{row['id']}")]
        for row in results
    ]
    if has_previous:
        rows.append([button("Previous", "sprev")])
    if len(results) == limit:
        rows.append([button("Next", f"snext:{results[-1]['id']}")])
    rows.append([button("Search again", "m:search"), button("Menu", "m:home")])
    text = f"Search results · up to {limit}\nQuery: {escape_markdown(query)}"
    if not results:
        text = "No matching contacts on this page."
    _edit_or_send(actions, update, text, InlineKeyboardMarkup(rows), ParseMode.MARKDOWN)


def _interested_screen(
    conn: Connection,
    update: Update,
    user_id: int,
    settings: Settings,
    actions: list[dict[str, Any]],
    after_id: UUID | None = None,
    history: list[str | None] | None = None,
) -> None:
    history = history or []
    rows = crm.interested_contacts(conn, settings.max_search_results, after_id, user_id)
    keyboard = [
        [button(f"{row['name'][:30]} · {row['interest_level']}", f"lead:{row['id']}")]
        for row in rows
    ]
    if history:
        keyboard.append([button("Previous", "iprev")])
    if len(rows) == settings.max_search_results:
        keyboard.append([button("Next", f"inext:{rows[-1]['id']}")])
    keyboard.append([button("Back", "m:home")])
    _set_input(
        conn,
        user_id,
        "INTERESTED_RESULTS",
        payload={"after_id": str(after_id) if after_id else None, "history": history},
    )
    text = "Interested prospects" if rows else "No more interested prospects on this page."
    _edit_or_send(actions, update, text, InlineKeyboardMarkup(keyboard))


def _campaign_picker(
    conn: Connection, update: Update, actions: list[dict[str, Any]], title: str
) -> None:
    campaigns = crm.active_campaigns(conn)
    keyboard = campaign_keyboard(campaigns, "camp")
    rows = [list(row) for row in keyboard.inline_keyboard]
    rows.insert(-1, [button("＋ Create Campaign", "camp:new")])
    _edit_or_send(actions, update, title, InlineKeyboardMarkup(rows))


def _followup_screen(
    conn: Connection,
    update: Update,
    user_id: int,
    prefs: dict[str, Any],
    actions: list[dict[str, Any]],
) -> None:
    rows = crm.recent_followups(conn, 20, user_id)
    text, _ = followups_keyboard(rows, prefs["timezone"])
    buttons = []
    now = datetime.now(UTC)
    for row in rows:
        buttons.append([button(f"Open {row['name'][:24]}", f"lead:{row['contact_id']}")])
        if row["scheduled_at"] <= now:
            buttons.append([button(f"✓ Complete · {row['name'][:20]}", f"fdone:{row['id']}")])
            buttons.append([button(f"Mark missed · {row['name'][:20]}", f"fmiss:{row['id']}")])
    buttons.append([button("Back", "m:home")])
    _edit_or_send(actions, update, text, InlineKeyboardMarkup(buttons), ParseMode.HTML)


def _session_lead(
    conn: Connection,
    session: dict[str, Any],
    user_id: int,
    settings: Settings,
    actions: list[dict[str, Any]],
    update: Update,
    advance: bool = False,
) -> None:
    if session.get("status") == "PAUSED":
        _edit_or_send(
            actions,
            update,
            "Your calling session is paused. Resume when you are ready.",
            InlineKeyboardMarkup(
                [
                    [button("Resume", f"resume:{session['id']}")],
                    [button("End Session", f"end:{session['id']}")],
                ]
            ),
        )
        return
    contact_id = session.get("current_contact_id")
    if contact_id and not advance:
        state = conn.execute(
            "select disposition from session_contacts where session_id=%s and contact_id=%s",
            (session["id"], contact_id),
        ).fetchone()
        if state and state["disposition"] == "PRESENTED":
            prefs = crm.ensure_user(conn, user_id, settings)
            _show_contact(
                conn, update, contact_id, user_id, prefs, actions, session["id"], settings
            )
            return
    candidate = crm.next_lead(conn, session["id"], user_id, settings)
    if candidate:
        prefs = crm.ensure_user(conn, user_id, settings)
        _show_contact(
            conn, update, candidate["id"], user_id, prefs, actions, session["id"], settings
        )
    else:
        _edit_or_send(
            actions,
            update,
            "No eligible prospects are available in this session right now.",
            InlineKeyboardMarkup(
                [
                    [button("Resume queue", f"resume:{session['id']}")],
                    [button("End Session", f"end:{session['id']}")],
                    [button("Menu", "m:home")],
                ]
            ),
        )


def _save_result_and_continue(
    conn: Connection,
    update: Update,
    *,
    contact_id: UUID,
    category: str,
    outcome: str,
    user_id: int,
    update_id: int,
    settings: Settings,
    actions: list[dict[str, Any]],
    note: str | None = None,
    ask_followup: bool | None = None,
    connection_result: str | None = None,
) -> None:
    pending = _get_input(conn, user_id)
    if connection_result is None and pending and pending.get("contact_id") == contact_id:
        connection_result = (pending.get("payload") or {}).get("connection_result")
        if pending["purpose"] == "ANSWERED_OUTCOME":
            _clear_input(conn, user_id)
    session = crm.current_session(conn, user_id)
    session_id = session["id"] if session else None
    campaign_id = (
        session["campaign_id"]
        if session and session.get("current_contact_id") == contact_id
        else None
    )
    saved = crm.save_call_result(
        conn,
        contact_id=contact_id,
        campaign_id=campaign_id,
        session_id=session_id,
        user_id=user_id,
        update_id=update_id,
        category=category,
        outcome=outcome,
        note=note,
        connection_result=connection_result,
    )
    if not saved:
        _edit_or_send(actions, update, "This call result was already recorded.", main_menu())
        return
    intelligence.recalculate_lead_score(
        conn, contact_id, intelligence.lead_weights_from_settings(settings)
    )
    should_offer = (
        ask_followup
        if ask_followup is not None
        else (
            outcome
            in {
                "NO_ANSWER",
                "BUSY",
                "PHONE_SWITCHED_OFF",
                "UNREACHABLE",
                "HIGHLY_INTERESTED",
                "INTERESTED",
                "QUALIFIED",
                "NEEDS_MORE_INFORMATION",
                "COMPARING_PROPERTIES",
                "PRICE_CONCERN",
                "PAYMENT_PLAN_CONCERN",
                "NOT_READY_YET",
                "REQUESTED_PROPERTY_VISIT",
                "MEETING_SCHEDULED",
                "CALL_BACK_TODAY",
                "CALL_BACK_TOMORROW",
                "CALL_BACK_IN_3_DAYS",
                "CALL_BACK_NEXT_WEEK",
                "SEND_INFORMATION",
                "ARRANGE_PROPERTY_VISIT",
                "SCHEDULE_MEETING",
                "WAIT_FOR_DECISION",
                "CUSTOM_DATE_TIME",
            }
        )
    )
    if outcome in {"PRICE_CONCERN", "PAYMENT_PLAN_CONCERN"} and not note:
        _set_input(
            conn, user_id, "OPTIONAL_NOTE", contact_id, {"category": category, "outcome": outcome}
        )
        _edit_or_send(
            actions,
            update,
            "Call result saved. Send a note about the concern, or send “-” to continue.",
        )
        return
    if outcome in {"WRONG_PERSON"}:
        _set_input(conn, user_id, "WRONG_PERSON_NOTE", contact_id)
        _edit_or_send(
            actions,
            update,
            "Call result saved. Add the corrected contact details, or send “-” to continue.",
        )
        return
    if outcome == "REQUESTED_WHATSAPP_MESSAGE":
        contact = crm.get_contact(conn, contact_id)
        prepared = f"Hello {contact['name']}, thank you for speaking with me. I can share property details that match your requirements. Which location or budget should I focus on?"
        _edit_or_send(
            actions,
            update,
            f"Call result saved. Copy this message into WhatsApp if you choose:\n\n<code>{html.escape(prepared)}</code>",
            InlineKeyboardMarkup([[button("Continue", f"continue:{contact_id}")]]),
            ParseMode.HTML,
        )
        return
    if outcome in {"OTHER", "OTHER_DISQUALIFICATION", "ALREADY_PURCHASED"} and not note:
        purpose = "ALREADY_PURCHASED_NOTE" if outcome == "ALREADY_PURCHASED" else "OTHER_NOTE"
        _set_input(conn, user_id, purpose, contact_id, {"outcome": outcome})
        prompt = "Call result saved. Add an optional note, referral, or future contact preference; send “-” to skip."
        if outcome in {"OTHER", "OTHER_DISQUALIFICATION"}:
            prompt = (
                "Call result saved. Add a short note about this outcome, or send “-” to continue."
            )
        _edit_or_send(actions, update, prompt)
        return
    if should_offer:
        _edit_or_send(
            actions,
            update,
            "Call result saved. Schedule a follow-up?",
            followup_keyboard(str(contact_id)),
        )
        return
    if session and session["status"] == "ACTIVE":
        session = crm.current_session(conn, user_id)
        if session:
            _session_lead(conn, session, user_id, settings, actions, update, advance=True)
            return
    _edit_or_send(actions, update, "Call result saved.", main_menu())


def _datetime_input(value: str, timezone: str) -> datetime:
    parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    local = parsed.replace(tzinfo=ZoneInfo(timezone))
    result = local.astimezone(UTC)
    if result <= datetime.now(UTC) - timedelta(minutes=1):
        raise ValueError("Choose a date and time in the future")
    return result


def _property_keyboard(contact_id: UUID) -> InlineKeyboardMarkup:
    values = [
        ("Apartment", "APARTMENT"),
        ("Commercial Shop", "COMMERCIAL_SHOP"),
        ("Office", "OFFICE"),
        ("House or Villa", "HOUSE_VILLA"),
        ("Land", "LAND"),
        ("Other", "OTHER"),
    ]
    return InlineKeyboardMarkup(
        [[button(label, f"pt:{contact_id}:{value}")] for label, value in values]
    )


def _start_property(
    conn: Connection,
    update: Update,
    user_id: int,
    contact_id: UUID,
    actions: list[dict[str, Any]],
    currency: str = "ETB",
) -> None:
    _set_input(
        conn,
        user_id,
        "PROPERTY",
        contact_id,
        {"step": "location", "values": {"currency": currency}},
    )
    _edit_or_send(actions, update, "Choose a property type.", _property_keyboard(contact_id))


def _campaign_select(
    conn: Connection,
    update: Update,
    user_id: int,
    token: str,
    settings: Settings,
    actions: list[dict[str, Any]],
) -> None:
    campaign_id = None if token == "all" else UUID(token)
    session = crm.start_or_resume_session(conn, user_id, campaign_id)
    if session.get("status") == "PAUSED":
        session = conn.execute(
            "update call_sessions set status='ACTIVE' where id=%s returning *", (session["id"],)
        ).fetchone()
    _session_lead(conn, session, user_id, settings, actions, update, advance=False)


def _handle_message(
    conn: Connection,
    update: Update,
    user_id: int,
    settings: Settings,
    update_id: int,
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    message = update.effective_message
    if not message:
        return actions
    text = (message.text or "").strip()
    invite_match = re.fullmatch(r"/start(?:@[A-Za-z0-9_]+)?\s+([A-Za-z0-9_-]{20,64})", text)
    if invite_match:
        actions.extend(
            phase4_handlers.handle_invitation_start(conn, update, user_id, invite_match.group(1))
        )
        return actions
    state = _get_input(conn, user_id)
    import_command = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
    is_text_import = bool(state and state.get("purpose") == "TEXT_IMPORT")
    try:
        team_service.authorize_message(
            conn,
            user_id,
            is_import=bool(message.document)
            or import_command in {"/import", "/textimport"}
            or is_text_import,
            is_read_only_command=text.startswith(("/start", "/menu", "/help", "/team")),
        )
    except PermissionError as exc:
        actions.append(_action_send(message.chat_id, str(exc)))
        return actions
    if message.document:
        doc = message.document
        suffix = Path(doc.file_name or "").suffix.lower()
        if suffix not in {".xlsx", ".csv"}:
            actions.append(_action_send(message.chat_id, "Upload an .xlsx or .csv contact list."))
            return actions
        if (doc.file_size or 0) > settings.max_import_file_mb * 1024 * 1024:
            actions.append(
                _action_send(
                    message.chat_id, f"File is over the {settings.max_import_file_mb} MB limit."
                )
            )
            return actions
        job_id = uuid4()
        base_name = Path(doc.file_name or "Imported Leads").stem[:100].strip() or "Imported Leads"
        campaign = crm.create_campaign(conn, base_name, "telegram-upload", actor_id=user_id)
        conn.execute(
            """insert into import_jobs (id,telegram_user_id,chat_id,telegram_file_id,original_filename,campaign_id,status)
               values (%s,%s,%s,%s,%s,%s,'RECEIVED')""",
            (
                job_id,
                user_id,
                message.chat_id,
                doc.file_id,
                (doc.file_name or "contacts.xlsx")[:255],
                campaign["id"],
            ),
        )
        actions.append(
            _action_send(
                message.chat_id,
                "File received. I’m checking its columns in the background; I’ll send the preview shortly.",
            )
        )
        return actions
    if not text:
        return actions
    if text.startswith("/"):
        command = text.split()[0].lower().split("@")[0]
        if command in {"/start", "/menu", "/help"}:
            _dashboard(conn, user_id, settings, actions, update)
        elif command == "/team":
            phase4_handlers.role_menu(conn, update, actions, user_id)
        elif command in {"/import", "/textimport"}:
            _set_input(conn, user_id, "TEXT_IMPORT")
            actions.append(
                _action_send(
                    message.chat_id,
                    "Paste lead rows in one message. Use tab-separated or CSV columns, or one lead per line. Include a header such as NO, Name, Phone when available. Ethiopian phones like +2519XXXXXXXX, 09XXXXXXXX, or 9XXXXXXXX are accepted, including spaces between digits. I’ll preview the matches before importing.",
                )
            )
        elif command == "/cancel" and is_text_import:
            _clear_input(conn, user_id)
            actions.append(_action_send(message.chat_id, "Text import cancelled."))
        return actions
    if state and state.get("purpose") == "TEXT_IMPORT":
        try:
            records, skipped = parse_pasted_leads(text, settings.max_import_rows)
        except ValueError as exc:
            actions.append(_action_send(message.chat_id, str(exc)))
            return actions
        job_id = uuid4()
        campaign_name = (
            f"Pasted Leads {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} {job_id.hex[:6]}"
        )
        campaign = crm.create_campaign(
            conn, campaign_name, "telegram-text-import", actor_id=user_id
        )
        conn.execute(
            """insert into import_jobs
                 (id,telegram_user_id,chat_id,original_filename,campaign_id,status)
               values (%s,%s,%s,%s,%s,'VALIDATING')""",
            (job_id, user_id, message.chat_id, "Pasted Telegram Leads.csv", campaign["id"]),
        )
        _clear_input(conn, user_id)
        actions.append(
            {
                "type": "stage_text_import",
                "job_id": str(job_id),
                "chat_id": message.chat_id,
                "filename": "pasted-telegram-leads.csv",
                "records": records,
                "skipped_lines": skipped,
            }
        )
        return actions
    if not state:
        mode = "name_phone"
        _set_input(
            conn,
            user_id,
            "SEARCH_RESULTS",
            payload={"query": text[:80], "mode": mode, "after_id": None, "history": []},
        )
        _search_results(
            conn,
            update,
            user_id,
            text,
            settings.max_search_results,
            actions,
            mode,
            settings.app_timezone,
        )
        return actions
    if state.get("purpose", "").startswith("P4_"):
        try:
            if phase4_handlers.handle_message(
                conn, update, user_id, settings, update_id, actions, state
            ):
                return actions
        except (PermissionError, ValueError, TypeError) as exc:
            actions.append(_action_send(message.chat_id, str(exc)))
            return actions
    if state.get("purpose", "").startswith("P3_"):
        phase3_handlers.handle_message(conn, update, user_id, settings, update_id, actions, state)
        return actions
    if state.get("purpose", "").startswith("P2_"):
        phase2_handlers.handle_message(conn, update, user_id, settings, update_id, actions, state)
        return actions
    purpose = state["purpose"]
    contact_id = state.get("contact_id")
    payload = state["payload"] or {}
    if purpose == "SEARCH":
        mode = payload.get("mode", "name_phone")
        _clear_input(conn, user_id)
        _set_input(
            conn,
            user_id,
            "SEARCH_RESULTS",
            payload={"query": text[:80], "mode": mode, "after_id": None, "history": []},
        )
        _search_results(
            conn,
            update,
            user_id,
            text,
            settings.max_search_results,
            actions,
            mode,
            settings.app_timezone,
        )
    elif purpose == "SEARCH_RESULTS":
        _clear_input(conn, user_id)
        _set_input(
            conn,
            user_id,
            "SEARCH_RESULTS",
            payload={"query": text[:80], "mode": "name_phone", "after_id": None, "history": []},
        )
        _search_results(
            conn,
            update,
            user_id,
            text,
            settings.max_search_results,
            actions,
            "name_phone",
            settings.app_timezone,
        )
    elif purpose == "SEARCH_FILTER":
        mode = payload.get("mode", "name_phone")
        value = text[:100]
        if mode == "status" and value.upper() not in {item.value for item in LeadStatus}:
            actions.append(
                _action_send(
                    message.chat_id, "Enter a valid lead status from the dashboard or contact card."
                )
            )
            return actions
        if mode == "interest" and value.upper() not in {
            "UNKNOWN",
            "LOW",
            "MEDIUM",
            "HIGH",
            "VERY_HIGH",
        }:
            actions.append(
                _action_send(message.chat_id, "Choose UNKNOWN, LOW, MEDIUM, HIGH, or VERY_HIGH.")
            )
            return actions
        _clear_input(conn, user_id)
        _set_input(
            conn,
            user_id,
            "SEARCH_RESULTS",
            payload={"query": value, "mode": mode, "after_id": None, "history": []},
        )
        _search_results(
            conn,
            update,
            user_id,
            value,
            settings.max_search_results,
            actions,
            mode,
            settings.app_timezone,
        )
    elif purpose == "NOTE" and contact_id:
        if text != "-":
            crm.save_note(conn, contact_id, text)
        _clear_input(conn, user_id)
        _show_contact(
            conn,
            update,
            contact_id,
            user_id,
            crm.ensure_user(conn, user_id, settings),
            actions,
            settings=settings,
        )
    elif purpose in {"OPTIONAL_NOTE", "WRONG_PERSON_NOTE"} and contact_id:
        if text != "-":
            crm.save_note(
                conn,
                contact_id,
                text,
                "CONTACT_CORRECTION" if purpose == "WRONG_PERSON_NOTE" else "CALL_RESULT",
            )
        _clear_input(conn, user_id)
        session = crm.current_session(conn, user_id)
        if session and session["status"] == "ACTIVE":
            _session_lead(conn, session, user_id, settings, actions, update, advance=True)
        else:
            _show_contact(
                conn,
                update,
                contact_id,
                user_id,
                crm.ensure_user(conn, user_id, settings),
                actions,
                settings=settings,
            )
    elif purpose in {"OTHER_NOTE", "ALREADY_PURCHASED_NOTE"} and contact_id:
        if text != "-":
            crm.save_note(
                conn,
                contact_id,
                text,
                "REFERRAL_OR_FUTURE_CONTACT"
                if purpose == "ALREADY_PURCHASED_NOTE"
                else "CALL_RESULT",
            )
        _clear_input(conn, user_id)
        session = crm.current_session(conn, user_id)
        if session and session["status"] == "ACTIVE":
            _session_lead(conn, session, user_id, settings, actions, update, advance=True)
        else:
            _show_contact(
                conn,
                update,
                contact_id,
                user_id,
                crm.ensure_user(conn, user_id, settings),
                actions,
                settings=settings,
            )
    elif purpose == "CUSTOM_FOLLOWUP" and contact_id:
        try:
            scheduled = _datetime_input(text, crm.ensure_user(conn, user_id, settings)["timezone"])
            crm.schedule_followup(
                conn,
                contact_id,
                payload.get("campaign_id"),
                scheduled,
                payload.get("kind", "CALL_BACK"),
                telegram_user_id=user_id,
            )
            _clear_input(conn, user_id)
            session = crm.current_session(conn, user_id)
            if session and session["status"] == "ACTIVE":
                _session_lead(conn, session, user_id, settings, actions, update, advance=True)
            else:
                _show_contact(
                    conn,
                    update,
                    contact_id,
                    user_id,
                    crm.ensure_user(conn, user_id, settings),
                    actions,
                    settings=settings,
                )
        except ValueError:
            actions.append(
                _action_send(
                    message.chat_id,
                    "Use `YYYY-MM-DD HH:MM` in your configured timezone, and choose a future time.",
                )
            )
    elif purpose == "MEETING_DATETIME" and contact_id:
        try:
            scheduled = _datetime_input(text, crm.ensure_user(conn, user_id, settings)["timezone"])
            _clear_input(conn, user_id)
            meeting_outcome = payload.get("outcome", "MEETING_SCHEDULED")
            _save_result_and_continue(
                conn,
                update,
                contact_id=contact_id,
                category="SALES_INTEREST",
                outcome=meeting_outcome,
                user_id=user_id,
                update_id=update_id,
                settings=settings,
                actions=actions,
                note=payload.get("note"),
                ask_followup=False,
                connection_result=payload.get("connection_result"),
            )
            session = crm.current_session(conn, user_id)
            crm.schedule_followup(
                conn,
                contact_id,
                session["campaign_id"] if session else None,
                scheduled,
                "PROPERTY_VISIT" if meeting_outcome == "REQUESTED_PROPERTY_VISIT" else "MEETING",
                telegram_user_id=user_id,
            )
            actions.append(
                _action_send(
                    message.chat_id,
                    f"Meeting saved for {local_label(scheduled, crm.ensure_user(conn, user_id, settings)['timezone'])}.",
                )
            )
        except ValueError:
            actions.append(
                _action_send(
                    message.chat_id,
                    "Use `YYYY-MM-DD HH:MM` in your configured timezone, and choose a future time.",
                )
            )
    elif purpose == "PROPERTY" and contact_id:
        _handle_property_answer(conn, message.chat_id, user_id, contact_id, state, text, actions)
    elif purpose == "DAILY_TARGET":
        try:
            target = int(text)
            if not 1 <= target <= 5000:
                raise ValueError
            conn.execute(
                "update user_settings set daily_call_target=%s,updated_at=now() where telegram_user_id=%s",
                (target, user_id),
            )
            _clear_input(conn, user_id)
            actions.append(
                _action_send(message.chat_id, f"Daily calling target set to {target}.", main_menu())
            )
        except ValueError:
            actions.append(_action_send(message.chat_id, "Enter a whole number from 1 to 5000."))
    elif purpose == "TIMEZONE":
        try:
            ZoneInfo(text)
            conn.execute(
                "update user_settings set timezone=%s,updated_at=now() where telegram_user_id=%s",
                (text, user_id),
            )
            _clear_input(conn, user_id)
            actions.append(
                _action_send(message.chat_id, f"Display timezone set to {text}.", main_menu())
            )
        except ZoneInfoNotFoundError:
            actions.append(
                _action_send(
                    message.chat_id, "Use a valid IANA timezone such as `Africa/Addis_Ababa`."
                )
            )
    elif purpose == "CURRENCY":
        currency = text.upper()
        if len(currency) == 3 and currency.isalpha():
            conn.execute(
                "update user_settings set currency=%s,updated_at=now() where telegram_user_id=%s",
                (currency, user_id),
            )
            _clear_input(conn, user_id)
            actions.append(
                _action_send(message.chat_id, f"Budget currency set to {currency}.", main_menu())
            )
        else:
            actions.append(
                _action_send(
                    message.chat_id, "Enter a three-letter currency code, such as ETB or USD."
                )
            )
    elif purpose == "CAMPAIGN_NAME":
        if not text.strip():
            actions.append(_action_send(message.chat_id, "Campaign name cannot be empty."))
        else:
            campaign = crm.create_campaign(conn, text.strip()[:120], "telegram", actor_id=user_id)
            _clear_input(conn, user_id)
            actions.append(
                _action_send(
                    message.chat_id,
                    f"Campaign created: {campaign['name']}.",
                    InlineKeyboardMarkup(
                        [
                            [button("Start Calling", f"camp:{campaign['id']}")],
                            [button("Menu", "m:home")],
                        ]
                    ),
                )
            )
    elif purpose == "EXPORT_DATES":
        try:
            parts = text.split()
            if len(parts) != 2:
                raise ValueError
            date_from = datetime.strptime(parts[0], "%Y-%m-%d").date()
            date_to = datetime.strptime(parts[1], "%Y-%m-%d").date()
            if date_to < date_from:
                raise ValueError
            _set_input(
                conn,
                user_id,
                "EXPORT_FORMAT",
                payload={
                    "date_from": date_from.isoformat(),
                    "date_to": (date_to + timedelta(days=1)).isoformat(),
                },
            )
            actions.append(
                _action_send(
                    message.chat_id, "Choose the export format.", export_date_format_keyboard()
                )
            )
        except ValueError:
            actions.append(
                _action_send(
                    message.chat_id,
                    "Enter two dates as `YYYY-MM-DD YYYY-MM-DD`, start then end.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            )
    else:
        _clear_input(conn, user_id)
        actions.append(
            _action_send(
                message.chat_id,
                "That input prompt expired. Open the menu to continue.",
                main_menu(),
            )
        )
    return actions


def _handle_property_answer(
    conn: Connection,
    chat_id: int,
    user_id: int,
    contact_id: UUID,
    state: dict[str, Any],
    text: str,
    actions: list[dict[str, Any]],
) -> None:
    payload = state["payload"] or {}
    values = payload.get("values", {})
    step = payload.get("step")
    prompts = {
        "location": ("Preferred location? Type a location or `-`.", "preferred_location"),
        "min_budget": ("Minimum budget? Enter a number or `-`.", "min_budget"),
        "max_budget": ("Maximum budget? Enter a number or `-`.", "max_budget"),
        "bedrooms": ("Bedrooms? Enter a number or `-`.", "bedrooms"),
        "payment": (
            "Preferred payment plan? Type a short answer or `-`.",
            "preferred_payment_plan",
        ),
        "readiness": ("Readiness to purchase? Type a short answer or `-`.", "readiness"),
        "timeline": (
            "Expected purchase timeline? Type a short answer or `-`.",
            "expected_timeline",
        ),
        "requirements": (
            "Additional requirements? Type a short answer or `-`.",
            "additional_requirements",
        ),
    }
    _, field = prompts.get(step, ("", ""))
    if text != "-":
        if field in {"min_budget", "max_budget"}:
            try:
                values[field] = Decimal(text.replace(",", ""))
                if values[field] < 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                actions.append(
                    _action_send(chat_id, "Enter a non-negative numeric amount, or `-`.")
                )
                return
        elif field == "bedrooms":
            try:
                values[field] = int(text)
                if values[field] < 0:
                    raise ValueError
            except ValueError:
                actions.append(_action_send(chat_id, "Enter a non-negative whole number, or `-`."))
                return
        else:
            values[field] = text[:300]
    order = list(prompts)
    next_index = order.index(step) + 1
    if next_index < len(order):
        next_step = order[next_index]
        payload.update({"step": next_step, "values": values})
        _set_input(conn, user_id, "PROPERTY", contact_id, payload)
        actions.append(_action_send(chat_id, prompts[next_step][0], parse_mode=ParseMode.MARKDOWN))
        return
    _clear_input(conn, user_id)
    try:
        validated = PropertyInterestInput(**values)
        crm.save_property_interest(conn, contact_id, validated.model_dump(), user_id=user_id)
        actions.append(_action_send(chat_id, "Property requirements saved."))
    except Exception as exc:
        logger.warning("Property-interest validation failed (%s)", type(exc).__name__)
        actions.append(
            _action_send(
                chat_id,
                "The property details could not be saved. Review the budget values and try again.",
            )
        )


def _quick_followup_time(choice: str, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    now = datetime.now(zone)
    if choice == "today":
        chosen = datetime.combine(now.date(), time(17, 0), tzinfo=zone)
        if chosen <= now:
            chosen = datetime.combine(now.date() + timedelta(days=1), time(9, 0), tzinfo=zone)
    elif choice == "tomorrow":
        chosen = datetime.combine(now.date() + timedelta(days=1), time(9, 0), tzinfo=zone)
    elif choice == "3days":
        chosen = datetime.combine(now.date() + timedelta(days=3), time(9, 0), tzinfo=zone)
    else:
        chosen = datetime.combine(now.date() + timedelta(days=7), time(9, 0), tzinfo=zone)
    return chosen.astimezone(UTC)


def _column_picker(job_id: str, headers: list[str], field: str) -> InlineKeyboardMarkup:
    rows = [
        [button(f"{i + 1}. {header[:30] or '(blank)'}", f"icol:{job_id}:{field}:{i}")]
        for i, header in enumerate(headers[:40])
    ]
    return InlineKeyboardMarkup(rows)


def _handle_callback(
    conn: Connection,
    update: Update,
    user_id: int,
    settings: Settings,
    update_id: int,
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    query = update.callback_query
    if not query:
        return actions
    data = expand_callback_data(query.data or "")
    actions.append(_action_answer(query.id))
    try:
        team_service.authorize_callback(conn, user_id, data)
    except PermissionError as exc:
        _edit_or_send(actions, update, str(exc), phase4_handlers.role_keyboard(conn, user_id))
        return actions
    if phase4_handlers.handle_callback(conn, update, user_id, settings, update_id, actions, data):
        return actions
    if phase3_handlers.handle_callback(conn, update, user_id, settings, update_id, actions, data):
        return actions
    if phase2_handlers.handle_callback(conn, update, user_id, settings, update_id, actions, data):
        return actions
    parts = data.split(":")
    prefix = parts[0] if parts else ""
    prefs = crm.ensure_user(conn, user_id, settings)
    try:
        if data in {"m:home", "m:dashboard"}:
            _dashboard(conn, user_id, settings, actions, update)
        elif data == "m:tools":
            phase2_handlers._sales_tools(
                update, actions, bool(getattr(settings, "ai_enabled", False))
            )
        elif data == "m:properties":
            phase2_handlers._inventory_menu(update, actions)
        elif data == "m:start" or data == "m:campaigns":
            title = "Choose a campaign to start calling. Your active session can be resumed."
            _campaign_picker(conn, update, actions, title)
        elif data == "m:next":
            session = crm.current_session(conn, user_id)
            if session:
                if session["status"] == "PAUSED":
                    conn.execute(
                        "update call_sessions set status='ACTIVE' where id=%s", (session["id"],)
                    )
                    session["status"] = "ACTIVE"
                _session_lead(conn, session, user_id, settings, actions, update)
            else:
                _campaign_picker(
                    conn, update, actions, "Choose a campaign to begin your calling session."
                )
        elif data == "m:import":
            _edit_or_send(
                actions,
                update,
                f"Upload an .xlsx or .csv file (up to {settings.max_import_file_mb} MB). I’ll preview up to 20 rows before you confirm.",
                InlineKeyboardMarkup(
                    [[button("Paste Leads", "m:textimport")], [button("Back", "m:home")]]
                ),
            )
        elif data == "m:textimport":
            _set_input(conn, user_id, "TEXT_IMPORT")
            _edit_or_send(
                actions,
                update,
                "Paste lead rows in one message. Use tab-separated or CSV columns, or one lead per line. Include a header such as NO, Name, Phone when available. Ethiopian phones like +2519XXXXXXXX, 09XXXXXXXX, or 9XXXXXXXX are accepted, including spaces between digits. I’ll preview the matches before importing. Send /cancel to stop.",
            )
        elif data == "m:search":
            _edit_or_send(actions, update, "Choose a search field.", search_keyboard())
        elif prefix == "sf" and len(parts) == 2:
            mode = parts[1]
            prompts = {
                "name_phone": "Send the contact name or full phone number.",
                "campaign": "Send the campaign name (or part of it).",
                "status": "Send a lead status, such as INTERESTED or FOLLOW_UP_REQUIRED.",
                "interest": "Send UNKNOWN, LOW, MEDIUM, HIGH, or VERY_HIGH.",
                "followup_date": "Send a follow-up date as YYYY-MM-DD.",
                "last_call_date": "Send a last-call date as YYYY-MM-DD.",
                "property_type": "Send APARTMENT, COMMERCIAL_SHOP, OFFICE, HOUSE_VILLA, LAND, or OTHER.",
                "location": "Send a preferred location or part of its name.",
                "budget": "Send minimum maximum currency, such as 1000000 3000000 ETB.",
            }
            if mode in prompts:
                _set_input(conn, user_id, "SEARCH", payload={"mode": mode})
                _edit_or_send(
                    actions,
                    update,
                    prompts[mode],
                    InlineKeyboardMarkup([[button("Cancel", "m:home")]]),
                )
        elif data == "m:followups":
            _followup_screen(conn, update, user_id, prefs, actions)
        elif data == "m:interested":
            _interested_screen(conn, update, user_id, settings, actions)
        elif data == "m:performance":
            _edit_or_send(
                actions,
                update,
                "Choose a reporting period. Rates use the definitions in the performance report.",
                performance_keyboard(),
            )
        elif data == "m:export":
            _edit_or_send(
                actions,
                update,
                "Choose an export filter. Results are bounded and sent only to your Telegram account.",
                export_keyboard(),
            )
        elif data == "m:settings":
            _edit_or_send(
                actions,
                update,
                f"⚙️ *Settings*\nTimezone: `{prefs['timezone']}`\nCurrency: `{prefs['currency']}`\n"
                f"Daily target: `{prefs['daily_call_target']}`\nAttempt limit: `{prefs['attempt_limit']}`",
                InlineKeyboardMarkup(
                    [
                        [
                            button("Daily target", "setting:target"),
                            button("Timezone", "setting:timezone"),
                        ],
                        [button("Currency", "setting:currency")],
                        [button("Back", "m:home")],
                    ]
                ),
                ParseMode.MARKDOWN,
            )
        elif prefix == "camp" and len(parts) == 2:
            if parts[1] == "new":
                _set_input(conn, user_id, "CAMPAIGN_NAME")
                _edit_or_send(actions, update, "Send a name for the new campaign.")
            else:
                _campaign_select(conn, update, user_id, parts[1], settings, actions)
        elif prefix == "lead" and len(parts) == 2:
            _show_contact(conn, update, UUID(parts[1]), user_id, prefs, actions, settings=settings)
        elif prefix == "call" and len(parts) == 2:
            contact = crm.get_contact(conn, UUID(parts[1]))
            if (
                contact
                and contact["contact_eligibility"] == "ELIGIBLE"
                and contact["normalized_phone"]
                and contact["lead_status"]
                not in {"DO_NOT_CONTACT", "NOT_INTERESTED", "DISQUALIFIED", "CONVERTED"}
                and contact.get("conversion_state") != "CONVERTED"
                and contact.get("qualification_status") != "DISQUALIFIED"
            ):
                actions.append(
                    {
                        "type": "dialer",
                        "chat_id": query.message.chat_id,
                        "name": contact["name"],
                        "tel_uri": tel_uri(contact["normalized_phone"]),
                    }
                )
            else:
                _edit_or_send(
                    actions,
                    update,
                    "This contact does not have a valid eligible number.",
                    main_menu(),
                )
        elif prefix == "h" and len(parts) == 2:
            contact_id = UUID(parts[1])
            history = crm.lead_history(conn, contact_id)
            _edit_or_send(
                actions,
                update,
                format_history(history, prefs["timezone"]),
                InlineKeyboardMarkup([[button("Back to Contact", f"lead:{contact_id}")]]),
                ParseMode.HTML,
            )
        elif prefix == "r" and len(parts) == 2:
            _edit_or_send(
                actions, update, "Select an outcome category.", outcome_categories(parts[1])
            )
        elif prefix == "cat" and len(parts) == 3:
            category = parts[2]
            labels = {
                "CONNECTION": "A · Connection result",
                "SALES_INTEREST": "B · Sales interest",
                "PROPERTY_REQUIREMENTS": "C · Property requirements",
                "FOLLOW_UP": "D · Follow-up or next action",
                "DISQUALIFICATION": "E · Disqualification or exclusion",
            }
            _edit_or_send(
                actions,
                update,
                labels.get(category, "Select an outcome."),
                outcomes_keyboard(parts[1], category),
            )
        elif prefix == "o" and len(parts) == 4:
            contact_id = UUID(parts[1])
            category, outcome = _decode_outcome_callback(parts[2], parts[3])
            if outcome == "ANSWERED" and category == "CONNECTION":
                _set_input(
                    conn, user_id, "ANSWERED_OUTCOME", contact_id, {"connection_result": "ANSWERED"}
                )
                _edit_or_send(
                    actions,
                    update,
                    "Answered. Choose the sales result or next action to save this call.",
                    outcomes_keyboard(str(contact_id), "SALES_INTEREST"),
                )
            elif outcome == "CONVERTED_TO_CUSTOMER":
                _edit_or_send(
                    actions,
                    update,
                    "Confirm conversion to a customer. Call history will be retained.",
                    InlineKeyboardMarkup(
                        [
                            [button("Confirm conversion", f"convert:{contact_id}")],
                            [button("Cancel", f"lead:{contact_id}")],
                        ]
                    ),
                )
            elif outcome == "DO_NOT_CONTACT":
                _edit_or_send(
                    actions,
                    update,
                    "Confirm Do Not Contact. This excludes the contact from calling queues.",
                    InlineKeyboardMarkup(
                        [
                            [
                                button(
                                    "Confirm exclusion",
                                    _call_confirmation_callback(contact_id, category, outcome),
                                )
                            ],
                            [button("Cancel", f"lead:{contact_id}")],
                        ]
                    ),
                )
            elif outcome in {
                "NO_ANSWER",
                "BUSY",
                "PHONE_SWITCHED_OFF",
                "UNREACHABLE",
                "CALL_FAILED",
            }:
                _edit_or_send(
                    actions,
                    update,
                    f"Confirm that you attempted this call and record *{outcome.replace('_', ' ').title()}*?",
                    InlineKeyboardMarkup(
                        [
                            [
                                button(
                                    "Confirm attempt",
                                    _call_confirmation_callback(contact_id, category, outcome),
                                )
                            ],
                            [button("Cancel", f"lead:{contact_id}")],
                        ]
                    ),
                    ParseMode.MARKDOWN,
                )
            elif outcome in {"MEETING_SCHEDULED", "REQUESTED_PROPERTY_VISIT"}:
                pending = _get_input(conn, user_id)
                connection_result = (
                    (pending.get("payload") or {}).get("connection_result")
                    if pending and pending.get("contact_id") == contact_id
                    else None
                )
                _set_input(
                    conn,
                    user_id,
                    "MEETING_DATETIME",
                    contact_id,
                    {"outcome": outcome, "connection_result": connection_result},
                )
                label = "meeting" if outcome == "MEETING_SCHEDULED" else "property visit"
                _edit_or_send(
                    actions,
                    update,
                    f"Enter the {label} date and time as `YYYY-MM-DD HH:MM` in {prefs['timezone']}.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            elif category == "PROPERTY_REQUIREMENTS":
                _save_result_and_continue(
                    conn,
                    update,
                    contact_id=contact_id,
                    category=category,
                    outcome=outcome,
                    user_id=user_id,
                    update_id=update_id,
                    settings=settings,
                    actions=actions,
                    ask_followup=False,
                )
                _start_property(conn, update, user_id, contact_id, actions, prefs["currency"])
            else:
                _save_result_and_continue(
                    conn,
                    update,
                    contact_id=contact_id,
                    category=category,
                    outcome=outcome,
                    user_id=user_id,
                    update_id=update_id,
                    settings=settings,
                    actions=actions,
                )
        elif prefix == "cc" and len(parts) == 3:
            contact_id = UUID(parts[1])
            try:
                category, outcome = _CALL_CONFIRMATION_CODES[parts[2]]
            except KeyError:
                raise ValueError("Unknown call confirmation") from None
            _save_result_and_continue(
                conn,
                update,
                contact_id=contact_id,
                category=category,
                outcome=outcome,
                user_id=user_id,
                update_id=update_id,
                settings=settings,
                actions=actions,
                ask_followup=outcome != "DO_NOT_CONTACT",
            )
        elif prefix == "confirmcall" and len(parts) == 4:
            contact_id, category, outcome = UUID(parts[1]), parts[2], parts[3]
            if outcome == "DO_NOT_CONTACT":
                _save_result_and_continue(
                    conn,
                    update,
                    contact_id=contact_id,
                    category=category,
                    outcome=outcome,
                    user_id=user_id,
                    update_id=update_id,
                    settings=settings,
                    actions=actions,
                    ask_followup=False,
                )
            else:
                _save_result_and_continue(
                    conn,
                    update,
                    contact_id=contact_id,
                    category=category,
                    outcome=outcome,
                    user_id=user_id,
                    update_id=update_id,
                    settings=settings,
                    actions=actions,
                )
        elif prefix == "convert" and len(parts) == 2:
            contact_id = UUID(parts[1])
            _save_result_and_continue(
                conn,
                update,
                contact_id=contact_id,
                category="SALES_INTEREST",
                outcome="CONVERTED_TO_CUSTOMER",
                user_id=user_id,
                update_id=update_id,
                settings=settings,
                actions=actions,
                ask_followup=False,
            )
        elif prefix == "dnc" and len(parts) == 2:
            contact_id = UUID(parts[1])
            _edit_or_send(
                actions,
                update,
                "Confirm Do Not Contact. The lead will leave every calling queue.",
                InlineKeyboardMarkup(
                    [
                        [button("Confirm DNC", f"dncok:{contact_id}")],
                        [button("Cancel", f"lead:{contact_id}")],
                    ]
                ),
            )
        elif prefix == "dncok" and len(parts) == 2:
            contact_id = UUID(parts[1])
            crm.set_do_not_contact(conn, contact_id, user_id, update_id)
            _show_contact(conn, update, contact_id, user_id, prefs, actions, settings=settings)
        elif prefix == "restore" and len(parts) == 2:
            contact_id = UUID(parts[1])
            _edit_or_send(
                actions,
                update,
                "Restore this contact's calling eligibility?",
                InlineKeyboardMarkup(
                    [
                        [button("Restore", f"restoreok:{contact_id}")],
                        [button("Cancel", f"lead:{contact_id}")],
                    ]
                ),
            )
        elif prefix == "restoreok" and len(parts) == 2:
            contact_id = UUID(parts[1])
            old = crm.get_contact(conn, contact_id)
            prior = conn.execute(
                "select old_value from audit_events where contact_id=%s and event_type='DO_NOT_CONTACT' order by created_at desc limit 1",
                (contact_id,),
            ).fetchone()
            status = (prior["old_value"] or {}).get("lead_status") if prior else None
            if old and old["contact_eligibility"] == "TEMPORARILY_PAUSED":
                status = old["lead_status"]
            elif not status or status in {"DO_NOT_CONTACT", "DISQUALIFIED"}:
                status = "FOLLOW_UP_REQUIRED"
            conn.execute(
                "update contacts set contact_eligibility='ELIGIBLE',lead_status=%s,updated_at=now() where id=%s",
                (status, contact_id),
            )
            conn.execute(
                "insert into audit_events (telegram_user_id,contact_id,event_type,old_value,new_value,telegram_update_id) values (%s,%s,'RESTORE_ELIGIBILITY',%s,%s,%s)",
                (
                    user_id,
                    contact_id,
                    {"eligibility": old["contact_eligibility"] if old else None},
                    {"eligibility": "ELIGIBLE"},
                    update_id,
                ),
            )
            _show_contact(conn, update, contact_id, user_id, prefs, actions, settings=settings)
        elif prefix == "pausecontact" and len(parts) == 2:
            contact_id = UUID(parts[1])
            _edit_or_send(
                actions,
                update,
                "Pause this contact in the automatic calling queue?",
                InlineKeyboardMarkup(
                    [
                        [button("Pause contact", f"pausecontactok:{contact_id}")],
                        [button("Cancel", f"lead:{contact_id}")],
                    ]
                ),
            )
        elif prefix == "pausecontactok" and len(parts) == 2:
            contact_id = UUID(parts[1])
            old = crm.get_contact(conn, contact_id)
            if old and old["contact_eligibility"] == "ELIGIBLE":
                conn.execute(
                    "update contacts set contact_eligibility='TEMPORARILY_PAUSED',updated_at=now() where id=%s",
                    (contact_id,),
                )
                conn.execute(
                    """insert into audit_events (telegram_user_id,contact_id,event_type,old_value,new_value,telegram_update_id)
                    values (%s,%s,'PAUSE_CALLING',%s,%s,%s)""",
                    (
                        user_id,
                        contact_id,
                        {"eligibility": "ELIGIBLE"},
                        {"eligibility": "TEMPORARILY_PAUSED"},
                        update_id,
                    ),
                )
            _show_contact(conn, update, contact_id, user_id, prefs, actions, settings=settings)
        elif prefix == "reopen" and len(parts) == 2:
            contact_id = UUID(parts[1])
            _edit_or_send(
                actions,
                update,
                "Reopen this lead for the calling queue?",
                InlineKeyboardMarkup(
                    [
                        [button("Reopen lead", f"reopenok:{contact_id}")],
                        [button("Cancel", f"lead:{contact_id}")],
                    ]
                ),
            )
        elif prefix == "reopenok" and len(parts) == 2:
            contact_id = UUID(parts[1])
            old = crm.get_contact(conn, contact_id)
            if old and old["lead_status"] == "NOT_INTERESTED":
                conn.execute(
                    "update contacts set lead_status='FOLLOW_UP_REQUIRED',updated_at=now() where id=%s",
                    (contact_id,),
                )
                conn.execute(
                    """insert into audit_events (telegram_user_id,contact_id,event_type,old_value,new_value,telegram_update_id)
                    values (%s,%s,'REOPEN_LEAD',%s,%s,%s)""",
                    (
                        user_id,
                        contact_id,
                        {"lead_status": "NOT_INTERESTED"},
                        {"lead_status": "FOLLOW_UP_REQUIRED"},
                        update_id,
                    ),
                )
            _show_contact(conn, update, contact_id, user_id, prefs, actions, settings=settings)
        elif prefix == "n" and len(parts) == 2:
            contact_id = UUID(parts[1])
            _set_input(conn, user_id, "NOTE", contact_id)
            _edit_or_send(
                actions,
                update,
                "Send the note text. Send `-` to cancel.",
                parse_mode=ParseMode.MARKDOWN,
            )
        elif prefix == "f" and len(parts) == 2:
            _edit_or_send(
                actions,
                update,
                "Choose a follow-up time. Quick options use 09:00 local time (17:00 for later today); custom time is available.",
                followup_keyboard(parts[1]),
            )
        elif prefix == "fq" and len(parts) == 3:
            contact_id = UUID(parts[1])
            scheduled = _quick_followup_time(parts[2], prefs["timezone"])
            session = crm.current_session(conn, user_id)
            campaign_id = session["campaign_id"] if session else None
            crm.schedule_followup(
                conn, contact_id, campaign_id, scheduled, "CALL_BACK", telegram_user_id=user_id
            )
            if session and session["status"] == "ACTIVE":
                selected = conn.execute(
                    "select disposition from session_contacts where session_id=%s and contact_id=%s",
                    (session["id"], contact_id),
                ).fetchone()
                if selected and selected["disposition"] == "PRESENTED":
                    crm.skip_current(conn, session["id"], user_id)
                _session_lead(conn, session, user_id, settings, actions, update, advance=True)
            else:
                _show_contact(conn, update, contact_id, user_id, prefs, actions, settings=settings)
        elif prefix == "fcustom" and len(parts) == 2:
            contact_id = UUID(parts[1])
            session = crm.current_session(conn, user_id)
            _set_input(
                conn,
                user_id,
                "CUSTOM_FOLLOWUP",
                contact_id,
                {
                    "campaign_id": str(session["campaign_id"])
                    if session and session["campaign_id"]
                    else None,
                    "kind": "CALL_BACK",
                },
            )
            _edit_or_send(
                actions,
                update,
                f"Enter the date and time as `YYYY-MM-DD HH:MM` in {prefs['timezone']}.",
                parse_mode=ParseMode.MARKDOWN,
            )
        elif prefix == "fdone" and len(parts) == 2:
            crm.complete_followup(conn, UUID(parts[1]), user_id)
            _followup_screen(conn, update, user_id, prefs, actions)
        elif prefix == "fmiss" and len(parts) == 2:
            crm.mark_followup_missed(conn, UUID(parts[1]), user_id)
            _followup_screen(conn, update, user_id, prefs, actions)
        elif prefix == "pt" and len(parts) == 3:
            contact_id, property_type = UUID(parts[1]), parts[2]
            state = _get_input(conn, user_id)
            payload = state["payload"] if state else {"values": {}}
            payload.setdefault("values", {})["property_type"] = property_type
            payload["step"] = "location"
            _set_input(conn, user_id, "PROPERTY", contact_id, payload)
            _edit_or_send(
                actions,
                update,
                "Preferred location? Type a location or `-`.",
                parse_mode=ParseMode.MARKDOWN,
            )
        elif prefix == "next" and len(parts) == 2:
            session_id = UUID(parts[1])
            selected = conn.execute(
                "select current_contact_id from call_sessions where id=%s and telegram_user_id=%s",
                (session_id, user_id),
            ).fetchone()
            if selected and selected["current_contact_id"]:
                current = conn.execute(
                    "select disposition from session_contacts where session_id=%s and contact_id=%s",
                    (session_id, selected["current_contact_id"]),
                ).fetchone()
                if current and current["disposition"] == "PRESENTED":
                    crm.skip_current(conn, session_id, user_id)
            session = conn.execute(
                "select * from call_sessions where id=%s and telegram_user_id=%s",
                (session_id, user_id),
            ).fetchone()
            if session:
                _session_lead(conn, session, user_id, settings, actions, update, advance=True)
        elif prefix == "skip" and len(parts) == 2:
            session_id = UUID(parts[1])
            crm.skip_current(conn, session_id, user_id)
            session = conn.execute(
                "select * from call_sessions where id=%s and telegram_user_id=%s",
                (session_id, user_id),
            ).fetchone()
            if session:
                _session_lead(conn, session, user_id, settings, actions, update, advance=True)
        elif prefix == "previous" and len(parts) == 2:
            session_id = UUID(parts[1])
            previous = crm.previous_lead(conn, session_id, user_id)
            if previous:
                conn.execute(
                    "update call_sessions set current_contact_id=%s where id=%s",
                    (previous["id"], session_id),
                )
                conn.execute(
                    "update session_contacts set disposition='PRESENTED',selected_at=now() where session_id=%s and contact_id=%s",
                    (session_id, previous["id"]),
                )
                _show_contact(
                    conn, update, previous["id"], user_id, prefs, actions, session_id, settings
                )
            else:
                _edit_or_send(
                    actions, update, "There is no skipped lead to return to.", main_menu()
                )
        elif prefix == "pause" and len(parts) == 2:
            session = conn.execute(
                "update call_sessions set status='PAUSED' where id=%s and telegram_user_id=%s returning *",
                (UUID(parts[1]), user_id),
            ).fetchone()
            if session:
                _session_lead(conn, session, user_id, settings, actions, update)
        elif prefix == "resume" and len(parts) == 2:
            session = conn.execute(
                "update call_sessions set status='ACTIVE' where id=%s and telegram_user_id=%s and status='PAUSED' returning *",
                (UUID(parts[1]), user_id),
            ).fetchone()
            if session:
                _session_lead(conn, session, user_id, settings, actions, update)
        elif prefix == "end" and len(parts) == 2:
            conn.execute(
                "update call_sessions set status='ENDED',ended_at=now() where id=%s and telegram_user_id=%s",
                (UUID(parts[1]), user_id),
            )
            _dashboard(conn, user_id, settings, actions, update)
        elif prefix == "continue" and len(parts) == 2:
            contact_id = UUID(parts[1])
            session = crm.current_session(conn, user_id)
            if session:
                _session_lead(conn, session, user_id, settings, actions, update, advance=True)
            else:
                _show_contact(conn, update, contact_id, user_id, prefs, actions, settings=settings)
        elif prefix == "snext" and len(parts) == 2:
            state = _get_input(conn, user_id)
            if state and state["purpose"] == "SEARCH_RESULTS":
                payload = state["payload"] or {}
                history = payload.get("history", []) + [payload.get("after_id")]
                after_id = UUID(parts[1])
                payload.update({"history": history, "after_id": str(after_id)})
                _set_input(conn, user_id, "SEARCH_RESULTS", payload=payload)
                _search_results(
                    conn,
                    update,
                    user_id,
                    payload["query"],
                    settings.max_search_results,
                    actions,
                    payload.get("mode", "name_phone"),
                    prefs["timezone"],
                    after_id,
                    bool(history),
                )
        elif data == "sprev":
            state = _get_input(conn, user_id)
            if state and state["purpose"] == "SEARCH_RESULTS":
                payload = state["payload"] or {}
                history = payload.get("history", [])
                if history:
                    after = history.pop()
                    payload.update({"history": history, "after_id": after})
                    _set_input(conn, user_id, "SEARCH_RESULTS", payload=payload)
                    _search_results(
                        conn,
                        update,
                        user_id,
                        payload["query"],
                        settings.max_search_results,
                        actions,
                        payload.get("mode", "name_phone"),
                        prefs["timezone"],
                        UUID(after) if after else None,
                        bool(history),
                    )
        elif prefix == "inext" and len(parts) == 2:
            state = _get_input(conn, user_id)
            if state and state["purpose"] == "INTERESTED_RESULTS":
                payload = state["payload"] or {}
                history = payload.get("history", []) + [payload.get("after_id")]
                _interested_screen(
                    conn, update, user_id, settings, actions, UUID(parts[1]), history
                )
        elif data == "iprev":
            state = _get_input(conn, user_id)
            if state and state["purpose"] == "INTERESTED_RESULTS":
                payload = state["payload"] or {}
                history = payload.get("history", [])
                if history:
                    after = history.pop()
                    _interested_screen(
                        conn,
                        update,
                        user_id,
                        settings,
                        actions,
                        UUID(after) if after else None,
                        history,
                    )
        elif prefix == "perf" and len(parts) == 2:
            period = {"day": "day", "week": "week", "month": "month"}.get(parts[1], "day")
            data = crm.report(conn, period, prefs["timezone"], user_id)
            _edit_or_send(
                actions, update, report_text(data), performance_keyboard(), ParseMode.MARKDOWN
            )
        elif prefix == "ex" and len(parts) == 2:
            if parts[1] == "campaign":
                campaigns = crm.active_campaigns(conn)
                rows = [
                    [button(campaign["name"][:50], f"excampaign:{campaign['id']}")]
                    for campaign in campaigns
                ]
                rows.append([button("Back", "m:export")])
                _edit_or_send(
                    actions, update, "Select a campaign to export.", InlineKeyboardMarkup(rows)
                )
            elif parts[1] == "date":
                _set_input(conn, user_id, "EXPORT_DATES")
                _edit_or_send(
                    actions,
                    update,
                    "Send the start and end dates as `YYYY-MM-DD YYYY-MM-DD`.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                _edit_or_send(
                    actions, update, "Choose the export format.", export_format_keyboard(parts[1])
                )
        elif prefix == "ef" and len(parts) in {3, 4}:
            crm.queue_export(
                conn,
                user_id=user_id,
                chat_id=query.message.chat_id,
                export_format=parts[2],
                filter_name=parts[1],
                campaign_id=UUID(parts[3]) if len(parts) == 4 else None,
                timezone=prefs["timezone"],
            )
            _edit_or_send(actions, update, "Export queued. I will send the file when it is ready.")
        elif prefix == "efd" and len(parts) == 2:
            state = _get_input(conn, user_id)
            if state and state["purpose"] == "EXPORT_FORMAT":
                crm.queue_export(
                    conn,
                    user_id=user_id,
                    chat_id=query.message.chat_id,
                    export_format=parts[1],
                    filter_name="all",
                    timezone=prefs["timezone"],
                    date_from=state["payload"]["date_from"],
                    date_to_exclusive=state["payload"]["date_to"],
                )
                _clear_input(conn, user_id)
                _edit_or_send(
                    actions, update, "Export queued. I will send the file when it is ready."
                )
        elif prefix == "setting" and len(parts) == 2:
            purpose = "DAILY_TARGET" if parts[1] == "target" else "TIMEZONE"
            if parts[1] == "currency":
                purpose = "CURRENCY"
            _set_input(conn, user_id, purpose)
            prompt = (
                "Enter your daily calling target (1–5000)."
                if purpose == "DAILY_TARGET"
                else "Enter an IANA timezone such as `Africa/Addis_Ababa`."
            )
            if purpose == "CURRENCY":
                prompt = "Enter a three-letter currency code such as ETB or USD. New property requirements will use it."
            _edit_or_send(actions, update, prompt, parse_mode=ParseMode.MARKDOWN)
        elif prefix == "iq" and len(parts) == 3:
            job = conn.execute(
                "select * from import_jobs where id=%s and telegram_user_id=%s for update",
                (UUID(parts[1]), user_id),
            ).fetchone()
            if job and job["status"] == "PREVIEW":
                conn.execute(
                    "update import_jobs set import_policy=%s,status='QUEUED',preview=null,updated_at=now() where id=%s",
                    (parts[2], job["id"]),
                )
                _edit_or_send(
                    actions,
                    update,
                    "Import confirmed and queued. I’ll process it in batches and send a final summary when the scheduled worker runs.",
                    main_menu(),
                )
        elif prefix == "icancel" and len(parts) == 2:
            conn.execute(
                "update import_jobs set status='CANCELLED',preview=null,header_mapping=null,completed_at=now(),updated_at=now() where id=%s and telegram_user_id=%s and status='PREVIEW'",
                (UUID(parts[1]), user_id),
            )
            _edit_or_send(actions, update, "Import cancelled.", main_menu())
        elif prefix == "icol" and len(parts) == 4:
            job_id, field, index = UUID(parts[1]), parts[2], int(parts[3])
            job = conn.execute(
                "select * from import_jobs where id=%s and telegram_user_id=%s and status='PREVIEW' for update",
                (job_id, user_id),
            ).fetchone()
            if job:
                preview = job["preview"] or {}
                headers = preview.get("headers", [])
                if field not in {"name", "phone"} or index < 0 or index >= len(headers):
                    _edit_or_send(
                        actions, update, "That column selection is no longer valid.", main_menu()
                    )
                else:
                    selected = preview.setdefault("selected", {})
                    selected[field] = index
                    conn.execute(
                        "update import_jobs set preview=%s,updated_at=now() where id=%s",
                        (json.dumps(preview), job_id),
                    )
                    missing = next((key for key in ("name", "phone") if key not in selected), None)
                    if missing:
                        _edit_or_send(
                            actions,
                            update,
                            f"Now select the {missing} column.",
                            _column_picker(str(job_id), headers, missing),
                        )
                    else:
                        actions.append(
                            {
                                "type": "finish_preview",
                                "job_id": str(job_id),
                                "storage_path": job["storage_path"],
                                "filename": job["original_filename"],
                                "mapping": selected,
                                "chat_id": query.message.chat_id,
                                "message_id": query.message.message_id,
                            }
                        )
        elif prefix == "campaign" and len(parts) == 2:
            _campaign_select(conn, update, user_id, parts[1], settings, actions)
        elif prefix == "excampaign" and len(parts) == 2:
            _edit_or_send(
                actions,
                update,
                "Choose the export format.",
                export_format_keyboard("eligible", parts[1]),
            )
    except (ValueError, TypeError) as exc:
        logger.info("Rejected malformed callback (%s)", type(exc).__name__)
        _edit_or_send(
            actions,
            update,
            "That action is no longer valid. Open the menu to continue.",
            main_menu(),
        )
    return actions


def dispatch_update(
    conn: Connection, update: Update, settings: Settings, update_id: int
) -> list[dict[str, Any]]:
    user = update.effective_user
    if not user:
        return []
    user_id = user.id
    if update.callback_query:
        return _handle_callback(conn, update, user_id, settings, update_id, [])
    return _handle_message(conn, update, user_id, settings, update_id, [])
