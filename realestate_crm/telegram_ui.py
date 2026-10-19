import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from html import escape
from typing import Any
from uuid import UUID

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from realestate_crm.timeutils import local_label

_CALLBACK_UUID = re.compile(
    r"(?i)(?<![0-9a-f])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])"
)
_PACKED_CALLBACK_UUID = re.compile(r"(?<=:)~([A-Za-z0-9_-]{22})(?=:|$)")
TELEGRAM_CALLBACK_DATA_MAX_BYTES = 64


def compact_callback_data(data: str) -> str:
    """Shorten UUID fields only when needed to fit Telegram's callback limit."""
    if not isinstance(data, str) or not data:
        raise ValueError("Telegram callback data must be a non-empty string")
    if len(data.encode("utf-8")) <= TELEGRAM_CALLBACK_DATA_MAX_BYTES:
        return data

    def pack(match: re.Match[str]) -> str:
        raw = UUID(match.group(1)).bytes
        token = urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return f"~{token}"

    compacted = _CALLBACK_UUID.sub(pack, data)
    if len(compacted.encode("utf-8")) > TELEGRAM_CALLBACK_DATA_MAX_BYTES:
        raise ValueError("Telegram callback data exceeds the 64-byte limit")
    return compacted


def expand_callback_data(data: str) -> str:
    """Restore UUID fields packed by compact_callback_data before routing."""
    if not isinstance(data, str) or "~" not in data:
        return data

    def unpack(match: re.Match[str]) -> str:
        token = match.group(1)
        try:
            raw = urlsafe_b64decode(token + "=" * ((-len(token)) % 4))
            return str(UUID(bytes=raw)) if len(raw) == 16 else match.group(0)
        except (ValueError, TypeError):
            return match.group(0)

    return _PACKED_CALLBACK_UUID.sub(unpack, data)


def button(text: str, data: str | None = None, url: str | None = None) -> InlineKeyboardButton:
    if url:
        return InlineKeyboardButton(text, url=url)
    return InlineKeyboardButton(
        text, callback_data=compact_callback_data(data) if data is not None else None
    )


def main_menu() -> InlineKeyboardMarkup:
    rows = [
        [button("▶️ Start Calling", "m:start"), button("📌 My Next Lead", "m:next")],
        [button("⏰ Follow-Ups", "m:followups"), button("⭐ Interested Buyers", "m:interested")],
        [button("📁 Campaigns", "m:campaigns"), button("🔎 Search Contacts", "m:search")],
        [button("📥 Import Excel", "m:import"), button("📊 Performance", "m:performance")],
        [button("📤 Export Data", "m:export"), button("⚙️ Settings", "m:settings")],
    ]
    rows.append([button("Sales Tools", "m:tools")])
    rows.append([button("Sales Pipeline", "p3:home"), button("Command Center", "p3:command")])
    rows.append(
        [button("Property Visits", "p3:visits"), button("Reactivation Queue", "p3:react:0")]
    )
    return InlineKeyboardMarkup(rows)


def search_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button("Name or phone", "sf:name_phone"), button("Campaign", "sf:campaign")],
            [button("Lead status", "sf:status"), button("Interest level", "sf:interest")],
            [
                button("Follow-up date", "sf:followup_date"),
                button("Last call date", "sf:last_call_date"),
            ],
            [button("Property type", "sf:property_type"), button("Location", "sf:location")],
            [button("Budget range", "sf:budget")],
            [button("Back", "m:home")],
        ]
    )


def dashboard_text(data: dict[str, Any]) -> str:
    return (
        f"🏠 *Calling dashboard · {data['period_label']}*\n\n"
        f"Contacts: *{data['total_contacts']:,}*  ·  Eligible: *{data['eligible_contacts']:,}*\n"
        f"Never called: *{data['never_called']:,}*\n\n"
        f"Calls today: *{data['calls_today']} / {data['daily_target']}* ({data['target_progress']}%)\n"
        f"Answered: *{data['answered_today']}*  ·  Interested: *{data['interested_today']}*\n"
        f"Follow-ups due: *{data['due_today']}*  ·  Overdue: *{data['overdue']}*\n"
        f"Meetings / visits arranged today: *{data['meetings_today']}*"
    )


def lead_text(contact: dict[str, Any], timezone: str) -> str:
    last = local_label(contact.get("last_called_at"), timezone)
    followup = local_label(contact.get("next_follow_up_at"), timezone)
    return (
        f"👤 <b>{escape(contact['name'])}</b>\n"
        f"☎️ {escape(contact['phone_number'])}\n"
        f"Campaign: {escape(contact.get('campaign_name') or '—')}\n"
        f"Status: {escape(str(contact['lead_status']))}  ·  Interest: {escape(str(contact['interest_level']))}\n"
        f"Eligibility: {escape(str(contact['contact_eligibility']))}\n"
        f"Qualification: {escape(str(contact.get('qualification_status') or 'UNQUALIFIED'))}  ·  "
        f"Priority: {escape(str(contact.get('lead_priority_category') or 'NEEDS_INFO'))} "
        f"({contact.get('lead_priority_score') if contact.get('lead_priority_score') is not None else 0}/100)\n"
        f"Attempts: {contact['total_call_attempts']}  ·  Last call: {last}\n"
        f"Follow-up: {followup}\n"
        f"Property interest: {escape(contact.get('property_interest') or '—')}\n"
        f"Latest note: {escape((contact.get('latest_note') or '—')[:240])}"
    )


def lead_keyboard(
    contact: dict[str, Any], session_id: str | None, ai_enabled: bool = False
) -> InlineKeyboardMarkup:
    rows = []
    contact_can_call = (
        contact.get("contact_eligibility") == "ELIGIBLE"
        and contact.get("lead_status")
        not in {"DO_NOT_CONTACT", "NOT_INTERESTED", "DISQUALIFIED", "CONVERTED"}
        and contact.get("conversion_state") != "CONVERTED"
        and contact.get("qualification_status") != "DISQUALIFIED"
    )
    if contact.get("normalized_phone") and contact_can_call:
        rows.append([button("📞 CALL NOW", f"call:{contact['id']}")])
    rows.extend(
        [
            [
                button("Record Call Result", f"r:{contact['id']}"),
                button("View History", f"h:{contact['id']}"),
            ],
            [
                button("Add Note", f"n:{contact['id']}"),
                button("Schedule Follow-Up", f"f:{contact['id']}"),
            ],
            [button("Property Requirements", f"p2:requirements:{contact['id']}")],
            [
                button("Matching Properties", f"p2:buyer_matches:{contact['id']}"),
                button("Qualify Buyer", f"p2:qualify:{contact['id']}"),
            ],
        ]
    )
    ai_contact_eligible = (
        contact.get("contact_eligibility") == "ELIGIBLE"
        and contact.get("lead_status")
        not in {"DO_NOT_CONTACT", "NOT_INTERESTED", "DISQUALIFIED", "CONVERTED"}
        and contact.get("conversion_state") != "CONVERTED"
        and contact.get("qualification_status") != "DISQUALIFIED"
    )
    if ai_enabled and ai_contact_eligible:
        rows.append(
            [
                button("Call Script", f"p2:script:{contact['id']}"),
                button("Objection Help", f"p2:objection:{contact['id']}"),
            ]
        )
        rows.append(
            [
                button("Structure Notes", f"p2:notes:{contact['id']}"),
                button("Follow-Up Draft", f"p2:draft:{contact['id']}"),
            ]
        )
    if contact["contact_eligibility"] == "DO_NOT_CONTACT":
        rows.append([button("Restore Calling Eligibility", f"restore:{contact['id']}")])
    elif contact["contact_eligibility"] == "INVALID_NUMBER":
        rows.append([button("Mark Number Valid", f"restore:{contact['id']}")])
    elif contact["contact_eligibility"] == "TEMPORARILY_PAUSED":
        rows.append([button("Resume Calling", f"restore:{contact['id']}")])
    elif contact["lead_status"] == "NOT_INTERESTED":
        rows.append([button("Reopen Lead", f"reopen:{contact['id']}")])
    elif contact["contact_eligibility"] == "ELIGIBLE":
        rows.append([button("Pause Calling", f"pausecontact:{contact['id']}")])
        rows.append([button("⛔ Mark Do Not Contact", f"dnc:{contact['id']}")])
    if session_id:
        rows.append(
            [
                button("⏭ Next Prospect", f"next:{session_id}"),
                button("⏸ Pause", f"pause:{session_id}"),
            ]
        )
        rows.append(
            [
                button("◀ Previous", f"previous:{session_id}"),
                button("Skip Lead", f"skip:{session_id}"),
            ]
        )
        rows.append([button("End Session", f"end:{session_id}")])
    rows.append([button("Back to Menu", "m:home")])
    return InlineKeyboardMarkup(rows)


def outcome_categories(contact_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button("A · Connection Result", f"cat:{contact_id}:CONNECTION")],
            [button("B · Sales Interest", f"cat:{contact_id}:SALES_INTEREST")],
            [button("C · Property Requirements", f"cat:{contact_id}:PROPERTY_REQUIREMENTS")],
            [button("D · Follow-Up / Next Action", f"cat:{contact_id}:FOLLOW_UP")],
            [button("E · Disqualification / Exclusion", f"cat:{contact_id}:DISQUALIFICATION")],
            [button("Cancel", f"lead:{contact_id}")],
        ]
    )


OUTCOMES: dict[str, list[tuple[str, str]]] = {
    "CONNECTION": [
        ("Answered", "ANSWERED"),
        ("No Answer", "NO_ANSWER"),
        ("Busy", "BUSY"),
        ("Phone Switched Off", "PHONE_SWITCHED_OFF"),
        ("Unreachable", "UNREACHABLE"),
        ("Invalid Number", "INVALID_NUMBER"),
        ("Call Failed", "CALL_FAILED"),
        ("Wrong Person", "WRONG_PERSON"),
        ("Requested WhatsApp Message", "REQUESTED_WHATSAPP_MESSAGE"),
        ("Other", "OTHER"),
    ],
    "SALES_INTEREST": [
        ("Highly Interested", "HIGHLY_INTERESTED"),
        ("Interested", "INTERESTED"),
        ("Mark Qualified", "QUALIFIED"),
        ("Needs More Information", "NEEDS_MORE_INFORMATION"),
        ("Comparing Properties", "COMPARING_PROPERTIES"),
        ("Price Concern", "PRICE_CONCERN"),
        ("Payment Plan Concern", "PAYMENT_PLAN_CONCERN"),
        ("Not Ready Yet", "NOT_READY_YET"),
        ("Requested Property Visit", "REQUESTED_PROPERTY_VISIT"),
        ("Meeting Scheduled", "MEETING_SCHEDULED"),
        ("Verbal Commitment", "VERBAL_COMMITMENT"),
        ("Converted to Customer", "CONVERTED_TO_CUSTOMER"),
    ],
    "PROPERTY_REQUIREMENTS": [("Record Property Requirements", "PROPERTY_REQUIREMENTS_RECORDED")],
    "FOLLOW_UP": [
        ("Call Back Today", "CALL_BACK_TODAY"),
        ("Call Back Tomorrow", "CALL_BACK_TOMORROW"),
        ("Call Back in 3 Days", "CALL_BACK_IN_3_DAYS"),
        ("Call Back Next Week", "CALL_BACK_NEXT_WEEK"),
        ("Send Information", "SEND_INFORMATION"),
        ("Arrange Property Visit", "ARRANGE_PROPERTY_VISIT"),
        ("Schedule Meeting", "SCHEDULE_MEETING"),
        ("Wait for Customer Decision", "WAIT_FOR_DECISION"),
        ("Custom Date and Time", "CUSTOM_DATE_TIME"),
    ],
    "DISQUALIFICATION": [
        ("Not Interested", "NOT_INTERESTED"),
        ("Budget Too Low", "BUDGET_TOO_LOW"),
        ("Wrong Target Customer", "WRONG_TARGET_CUSTOMER"),
        ("Already Purchased", "ALREADY_PURCHASED"),
        ("Duplicate Contact", "DUPLICATE_CONTACT"),
        ("Invalid Contact", "INVALID_CONTACT"),
        ("Do Not Contact", "DO_NOT_CONTACT"),
        ("Other", "OTHER_DISQUALIFICATION"),
    ],
}

OUTCOME_CATEGORY_CODES = {
    "CONNECTION": "c",
    "SALES_INTEREST": "s",
    "PROPERTY_REQUIREMENTS": "p",
    "FOLLOW_UP": "f",
    "DISQUALIFICATION": "d",
}
OUTCOME_CODE_CATEGORIES = {code: category for category, code in OUTCOME_CATEGORY_CODES.items()}


def outcomes_keyboard(contact_id: str, category: str) -> InlineKeyboardMarkup:
    choices = OUTCOMES.get(category, [])
    category_code = OUTCOME_CATEGORY_CODES.get(category, "?")
    rows = [
        [button(label, f"o:{contact_id}:{category_code}:{index}")]
        for index, (label, _code) in enumerate(choices)
    ]
    rows.append([button("‹ Categories", f"r:{contact_id}")])
    return InlineKeyboardMarkup(rows)


def campaign_keyboard(
    campaigns: list[dict[str, Any]], callback_prefix: str = "campaign"
) -> InlineKeyboardMarkup:
    rows = [
        [button(campaign["name"][:50], f"{callback_prefix}:{campaign['id']}")]
        for campaign in campaigns
    ]
    rows.append([button("All Campaigns", f"{callback_prefix}:all")])
    rows.append([button("Back to Menu", "m:home")])
    return InlineKeyboardMarkup(rows)


def followup_keyboard(contact_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                button("Later today", f"fq:{contact_id}:today"),
                button("Tomorrow", f"fq:{contact_id}:tomorrow"),
            ],
            [
                button("In 3 days", f"fq:{contact_id}:3days"),
                button("Next week", f"fq:{contact_id}:week"),
            ],
            [button("Custom date & time", f"fcustom:{contact_id}")],
            [button("Skip", f"lead:{contact_id}")],
        ]
    )


def format_history(rows: list[dict[str, Any]], timezone: str) -> str:
    if not rows:
        return "No call history has been recorded for this contact."
    lines = ["📜 *Recent call history*"]
    for row in rows:
        when = local_label(row["created_at"], timezone)
        lines.append(
            f"• {when} · {escape(row['outcome_category'])} / {escape(row['outcome_code'])}"
        )
        if row.get("notes"):
            lines.append(f"  <i>{escape(row['notes'][:180])}</i>")
    return "\n".join(lines)


def preview_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button("Add existing leads to this campaign", f"iq:{job_id}:ADD_TO_CAMPAIGN")],
            [button("Skip existing contacts", f"iq:{job_id}:SKIP_EXISTING")],
            [button("Update existing contact details", f"iq:{job_id}:UPDATE_EXISTING")],
            [button("Cancel import", f"icancel:{job_id}")],
        ]
    )


def render_preview(
    filename: str, preview: list[dict[str, str]], job_id: str
) -> tuple[str, InlineKeyboardMarkup]:
    sample = (
        "\n".join(
            f"• {escape(row['name'][:32])} · {escape(row['phone'][:28])}" for row in preview[:20]
        )
        or "No data rows found."
    )
    return (
        f"📄 <b>Preview: {escape(filename[:100])}</b>\nShowing up to 20 rows:\n{sample}\n\n"
        "Choose how to handle phone numbers already in the CRM. Import runs in background batches. "
        "Invalid rows and duplicates will be counted in the final report.",
        preview_keyboard(job_id),
    )


def export_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button("Eligible contacts", "ex:eligible"), button("Never called", "ex:uncalled")],
            [
                button("Interested prospects", "ex:interested"),
                button("Converted customers", "ex:converted"),
            ],
            [button("Follow-ups due", "ex:due"), button("Selected campaign", "ex:campaign")],
            [button("Date range", "ex:date"), button("All contacts", "ex:all")],
            [button("Back", "m:home")],
        ]
    )


def export_format_keyboard(
    filter_name: str, campaign_id: str | None = None
) -> InlineKeyboardMarkup:
    suffix = f":{campaign_id}" if campaign_id else ""
    return InlineKeyboardMarkup(
        [
            [
                button("Download CSV", f"ef:{filter_name}:csv{suffix}"),
                button("Download XLSX", f"ef:{filter_name}:xlsx{suffix}"),
            ],
            [button("Cancel", "m:export")],
        ]
    )


def export_date_format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button("Download CSV", "efd:csv"), button("Download XLSX", "efd:xlsx")],
            [button("Cancel", "m:export")],
        ]
    )


def performance_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                button("Today", "perf:day"),
                button("This week", "perf:week"),
                button("This month", "perf:month"),
            ],
            [button("Back", "m:home")],
        ]
    )


def report_text(data: dict[str, Any]) -> str:
    return (
        f"📊 *{data['period'].title()} performance · {data['start']}–{data['end']}*\n\n"
        f"Call attempts: {data['attempted']}  ·  Unique contacts: {data['unique_called']}\n"
        f"Answered: {data['answered']} ({data['answer_rate']}%)  ·  No answer: {data['no_answer']}\n"
        f"Interested: {data['interested']} ({data['interest_rate']}% of answers)\n"
        f"Qualified: {data['qualified']} prospects\n"
        f"Meetings: {data['meetings']}  ·  Property visits: {data['visits']}\n"
        f"Converted: {data['converted']} ({data['conversion_rate']}% of unique contacts)\n"
        f"Follow-ups: {data['scheduled']} scheduled · {data['due']} due · {data['completed']} completed · {data['overdue']} overdue\n"
        f"Follow-up completion: {data['followup_completion_rate']}%"
    )


def followups_keyboard(
    rows: list[dict[str, Any]], timezone: str
) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["⏰ <b>Upcoming follow-ups</b>"]
    buttons = []
    for row in rows:
        lines.append(
            f"• {local_label(row['scheduled_at'], timezone)} · {escape(row['name'])} · {escape(row['follow_up_type'])}"
        )
        buttons.append([button(f"Open {row['name'][:32]}", f"lead:{row['contact_id']}")])
    if len(lines) == 1:
        lines.append("No follow-ups are scheduled yet.")
    buttons.append([button("Back", "m:home")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)
