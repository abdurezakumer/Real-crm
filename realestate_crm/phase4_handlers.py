"""Telegram entry points for team administration and Phase 4 workflows."""

from __future__ import annotations

import html
import json
import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg import Connection
from telegram import InlineKeyboardMarkup, Update

from realestate_crm.config import Settings
from realestate_crm.services import crm, team
from realestate_crm.telegram_ui import button

logger = logging.getLogger(__name__)


def _set_state(
    conn: Connection,
    user_id: int,
    purpose: str,
    *,
    payload: dict[str, Any] | None = None,
    contact_id: UUID | None = None,
) -> None:
    conn.execute(
        """insert into user_input_state(telegram_user_id,purpose,payload,contact_id,expires_at)
           values (%s,%s,%s::jsonb,%s,now()+interval '30 minutes')
           on conflict(telegram_user_id) do update set purpose=excluded.purpose,
             payload=excluded.payload,contact_id=excluded.contact_id,expires_at=excluded.expires_at,updated_at=now()""",
        (user_id, purpose, json.dumps(payload or {}), contact_id),
    )


def _clear_state(conn: Connection, user_id: int) -> None:
    conn.execute("delete from user_input_state where telegram_user_id=%s", (user_id,))


def _state(conn: Connection, user_id: int) -> dict[str, Any] | None:
    return conn.execute(
        "select * from user_input_state where telegram_user_id=%s and expires_at>now()",
        (user_id,),
    ).fetchone()


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
        actions.append(
            {
                "type": "send",
                "chat_id": message.chat_id,
                "text": text,
                "markup": markup,
                "parse_mode": "HTML",
            }
        )


def _menu(conn: Connection, update: Update, actions: list[dict[str, Any]], user_id: int) -> None:
    actor = team.get_user(conn, user_id)
    if not actor or actor["account_status"] != "ACTIVE":
        raise PermissionError("This CRM account is inactive. Contact the administrator.")
    role = actor["role"]
    rows: list[list[Any]] = []
    if role == "SUPER_ADMIN":
        rows = [
            [button("Invite Team Member", "p4:invite"), button("Team Members", "p4:members:0")],
            [button("Invitations", "p4:invitations")],
            [button("Teams", "p4:teams"), button("Assign Leads", "p4:assign")],
            [button("Lead Distribution", "p4:dist")],
            [button("Calling Campaigns", "p4:campaigns")],
            [button("Handover Requests", "p4:handovers")],
            [button("Team Performance", "p4:performance")],
            [button("Targets", "p4:targets"), button("Sales & Commissions", "p3:commissions")],
            [button("Audit Logs", "p4:audit"), button("Team Settings", "p4:settings")],
            [button("Sales Pipeline", "p3:home"), button("Command Center", "p3:command")],
            [button("Main Menu", "m:home")],
        ]
    elif role == "SALES_MANAGER":
        rows = [
            [button("Team Members", "p4:members:0"), button("Assign Leads", "p4:assign")],
            [button("Team Calling Campaigns", "p4:campaigns")],
            [button("Handover Requests", "p4:handovers")],
            [button("Team Performance", "p4:performance"), button("Team Targets", "p4:targets")],
            [button("Team Audit Log", "p4:audit")],
            [button("Sales Pipeline", "p3:home"), button("My Calling Queue", "m:start")],
            [button("Main Menu", "m:home")],
        ]
    elif role == "SALESPERSON":
        rows = [
            [button("My Calling Queue", "m:start"), button("My Follow-Ups", "m:followups")],
            [button("My Opportunities", "p3:home"), button("My Performance", "p4:performance")],
            [button("My Targets", "p4:targets"), button("My Commission", "p4:myfinance")],
            [button("Request Lead Handover", "p4:handover")],
            [button("Main Menu", "m:home")],
        ]
    elif role == "MARKETING_ADMIN":
        rows = [
            [button("Campaigns", "m:campaigns"), button("Import Leads", "m:import")],
            [button("Campaign Reports", "p4:campaigns")],
            [button("Main Menu", "m:home")],
        ]
    else:
        rows = [
            [button("Authorized Team Reports", "p4:performance")],
            [button("Main Menu", "m:home")],
        ]
    _edit(
        update,
        actions,
        f"<b>Team Workspace</b> · {html.escape(role.replace('_', ' ').title())}",
        InlineKeyboardMarkup(rows),
    )


def role_keyboard(conn: Connection, user_id: int) -> InlineKeyboardMarkup:
    actor = team.get_user(conn, user_id)
    role = actor["role"] if actor and actor["account_status"] == "ACTIVE" else "SALESPERSON"
    if role == "SUPER_ADMIN":
        rows = [
            [button("Start Calling", "m:start"), button("My Next Lead", "m:next")],
            [button("Follow-Ups", "m:followups"), button("Campaigns", "m:campaigns")],
            [button("Import Excel", "m:import"), button("Paste Leads", "m:textimport")],
            [button("Export Data", "m:export")],
            [button("Team Management", "p4:menu")],
            [button("Calling Campaigns", "p4:campaigns")],
            [button("Sales Pipeline", "p3:home"), button("Command Center", "p3:command")],
        ]
    elif role == "SALES_MANAGER":
        rows = [
            [button("My Calling Queue", "m:start"), button("Team Follow-Ups", "m:followups")],
            [button("My Assigned Leads", "m:search")],
            [button("Team Management", "p4:menu")],
            [button("Team Sales Pipeline", "p3:home")],
        ]
    elif role == "SALESPERSON":
        rows = [
            [button("My Calling Queue", "m:start"), button("My Next Lead", "m:next")],
            [button("My Follow-Ups", "m:followups"), button("My Leads", "m:search")],
            [button("My Opportunities", "p3:home")],
            [button("Team Workspace", "p4:menu")],
        ]
    elif role == "MARKETING_ADMIN":
        rows = [
            [button("Campaigns", "m:campaigns"), button("Import Leads", "m:import")],
            [button("Paste Leads", "m:textimport")],
            [button("Campaign Workspace", "p4:menu")],
        ]
    else:
        rows = [[button("Team Reports", "p4:performance")], [button("Workspace", "p4:menu")]]
    return InlineKeyboardMarkup(rows)


def _teams_page(
    conn: Connection, update: Update, actions: list[dict[str, Any]], user_id: int
) -> None:
    rows = team.list_teams(conn, user_id)
    keyboard = [
        [button(f"{item['name'][:30]} · {item['member_count']} members", f"p4:team:{item['id']}")]
        for item in rows
    ]
    actor = team.get_user(conn, user_id)
    if actor["role"] == "SUPER_ADMIN":
        keyboard.append([button("Create Team", "p4:teamnew")])
    keyboard.append([button("Team Workspace", "p4:menu")])
    _edit(
        update,
        actions,
        "<b>Teams</b>\nActive teams and their member counts.",
        InlineKeyboardMarkup(keyboard),
    )


def _audit_page(
    conn: Connection,
    update: Update,
    actions: list[dict[str, Any]],
    user_id: int,
    *,
    subject_user_id: int | None = None,
) -> None:
    events = team.list_audit_events(conn, user_id, limit=20, subject_user_id=subject_user_id)
    title = "Member Activity" if subject_user_id is not None else "Recent Authorized Audit Events"
    lines = [f"<b>{title}</b>"]
    for event in events:
        actor_name = event["actor_name"] or str(event["actor_id"])
        subject = event["contact_name"] or event["entity_type"]
        reason = f" · {event['reason'][:100]}" if event.get("reason") else ""
        lines.append(
            f"\n{html.escape(str(event['created_at'])[:16])} · "
            f"<b>{html.escape(event['event_type'].replace('_', ' ').title())}</b>\n"
            f"{html.escape(str(actor_name)[:50])} · {html.escape(str(subject)[:70])}{html.escape(reason)}"
        )
    if not events:
        lines.append("\nNo audit events are available in your authorized scope.")
    back = "p4:menu"
    _edit(update, actions, "\n".join(lines), InlineKeyboardMarkup([[button("Workspace", back)]]))


def _team_settings_page(
    conn: Connection, update: Update, actions: list[dict[str, Any]], user_id: int
) -> None:
    settings = team.get_team_settings(conn, user_id)
    toggles = (
        ("unassigned_visible_to_salespeople", "Salespeople see unassigned leads"),
        ("salespeople_can_request_reassignment", "Allow reassignment requests"),
        ("manager_can_override_ownership", "Managers can override ownership"),
        ("active_negotiation_requires_super_admin", "Negotiation handover needs super admin"),
    )
    rows = [
        [button(f"{'ON' if settings[key] else 'OFF'} · {label}", f"p4:setting:{key}")]
        for key, label in toggles
    ]
    rows.extend(
        [
            [
                button(
                    f"Active lead capacity · {settings['max_active_leads_per_salesperson']}",
                    "p4:settingcapacity",
                )
            ],
            [
                button(
                    f"Distribution · {settings['default_distribution_method'].replace('_', ' ').title()}",
                    "p4:settingmethod",
                )
            ],
            [button("Workspace", "p4:menu")],
        ]
    )
    _edit(
        update,
        actions,
        "<b>Team Settings</b>\nSelect a setting to change it.",
        InlineKeyboardMarkup(rows),
    )


def _handovers_page(
    conn: Connection, update: Update, actions: list[dict[str, Any]], user_id: int
) -> None:
    requests = team.list_handover_requests(conn, user_id, limit=20)
    rows = [
        [
            button(
                f"{item['contact_name'][:25]} · {item['reason'].replace('_', ' ').title()}",
                f"p4:handoverrequest:{item['id']}",
            )
        ]
        for item in requests
    ]
    rows.append([button("Workspace", "p4:menu")])
    text = "<b>Pending Lead Handover Requests</b>"
    if not requests:
        text += "\nNo pending requests are in your authorized scope."
    _edit(update, actions, text, InlineKeyboardMarkup(rows))


def _members_page(
    conn: Connection, update: Update, actions: list[dict[str, Any]], user_id: int, page: int
) -> None:
    members = team.list_members(conn, user_id, limit=100)
    start = max(0, min(page, max(0, (len(members) - 1) // 8))) * 8
    current = members[start : start + 8]
    rows = [
        [
            button(
                f"{m['display_name'] or m['telegram_user_id']} · {m['role'].replace('_', ' ')}",
                f"p4:member:{m['telegram_user_id']}",
            )
        ]
        for m in current
    ]
    if start:
        rows.append([button("Previous", f"p4:members:{page - 1}")])
    if start + 8 < len(members):
        rows.append([button("Next", f"p4:members:{page + 1}")])
    rows.append([button("Team Workspace", "p4:menu")])
    _edit(
        update,
        actions,
        f"<b>Team Members</b> · {len(members)} authorized accounts",
        InlineKeyboardMarkup(rows),
    )


def _invitations_page(
    conn: Connection, update: Update, actions: list[dict[str, Any]], user_id: int
) -> None:
    invitations = team.list_invitations(conn, user_id, limit=20)
    lines = ["<b>Recent Invitations</b>"]
    rows = []
    for invitation in invitations:
        team_name = invitation["team_name"] or "No default team"
        lines.append(
            f"\n{html.escape(invitation['role'].replace('_', ' ').title())} · "
            f"{html.escape(team_name)} · {html.escape(invitation['status'])}\n"
            f"Expires {html.escape(str(invitation['expires_at'])[:16])}"
        )
        if invitation["status"] == "PENDING":
            rows.append(
                [
                    button(
                        f"Revoke {invitation['role'].replace('_', ' ').title()}",
                        f"p4:inviterevoke:{invitation['id']}",
                    )
                ]
            )
    if not invitations:
        lines.append("\nNo invitations have been created.")
    rows.append([button("Invite Member", "p4:invite")])
    rows.append([button("Workspace", "p4:menu")])
    _edit(update, actions, "\n".join(lines), InlineKeyboardMarkup(rows))


def _member_card(
    conn: Connection, update: Update, actions: list[dict[str, Any]], actor_id: int, member_id: int
) -> None:
    actor = team.get_user(conn, actor_id)
    if actor["role"] == "SALES_MANAGER":
        team.require_member_scope(conn, actor_id, member_id)
    elif actor["role"] != "SUPER_ADMIN":
        raise PermissionError("Member details are not available to this role.")
    row = conn.execute(
        """select u.*,array_remove(array_agg(distinct t.name),null) team_names,
                  count(distinct c.id) assigned_leads
           from crm_users u left join crm_team_memberships m on m.telegram_user_id=u.telegram_user_id and m.active
           left join crm_teams t on t.id=m.team_id left join contacts c on c.assigned_telegram_user_id=u.telegram_user_id
           where u.telegram_user_id=%s group by u.telegram_user_id""",
        (member_id,),
    ).fetchone()
    if not row:
        raise ValueError("Team member was not found")
    text = (
        f"<b>{html.escape(row['display_name'] or str(member_id))}</b>\n"
        f"Role: {html.escape(row['role'].replace('_', ' ').title())}\n"
        f"Status: {html.escape(row['account_status'])}\n"
        f"Teams: {html.escape(', '.join(row['team_names'] or []) or 'None')}\n"
        f"Assigned leads: {row['assigned_leads']}"
    )
    rows = []
    rows.append([button("Member Activity", f"p4:memberactivity:{member_id}")])
    rows.append([button("Member Performance", f"p4:memberperf:{member_id}")])
    if actor["role"] == "SUPER_ADMIN" and row["role"] != "SUPER_ADMIN":
        rows.append([button("Change Role", f"p4:rolechange:{member_id}")])
        rows.append([button("Assign to Team", f"p4:memberteams:{member_id}")])
        if row["account_status"] == "ACTIVE":
            rows.append([button("Suspend Member", f"p4:suspend:{member_id}")])
        else:
            rows.append([button("Reactivate Member", f"p4:reactivate:{member_id}")])
    rows.extend([[button("Members", "p4:members:0")], [button("Team Workspace", "p4:menu")]])
    _edit(update, actions, text, InlineKeyboardMarkup(rows))


def _invite_role(
    conn: Connection, update: Update, actions: list[dict[str, Any]], user_id: int, role: str
) -> None:
    team.require_permission(conn, user_id, "users.manage")
    roles = ("SALES_MANAGER", "SALESPERSON", "MARKETING_ADMIN", "OBSERVER", "CUSTOM")
    if role not in roles:
        raise ValueError("Choose a supported invite role")
    teams = team.list_teams(conn, user_id)
    rows = [[button("No default team", f"p4:inviteconfirm:{role}:none")]]
    rows.extend(
        [button(t["name"][:32], f"p4:inviteconfirm:{role}:{t['id']}")] for t in teams if t["active"]
    )
    _edit(
        update,
        actions,
        f"Invite a <b>{html.escape(role.replace('_', ' ').title())}</b>. Select their default team. This invitation expires in 3 days and can be used once.",
        InlineKeyboardMarkup(rows),
    )


def _distribution_preview(
    conn: Connection, update: Update, actions: list[dict[str, Any]], job_id: UUID
) -> None:
    job = conn.execute("select * from lead_assignment_jobs where id=%s", (job_id,)).fetchone()
    if not job or job["status"] != "PREVIEW":
        raise ValueError("Assignment preview is no longer available")
    counts: dict[str, int] = {}
    for item in job["preview_assignments"]:
        label = item["owner_name"] or str(item["owner_id"])
        counts[label] = counts.get(label, 0) + 1
    distribution = "\n".join(f"· {html.escape(name)}: {n}" for name, n in counts.items())
    filters = html.escape(json.dumps(job["filters"], ensure_ascii=False)[:300])
    rows = [
        [button(f"Confirm {job['selected_count']} assignments", f"p4:distconfirm:{job_id}")],
        [button("Cancel Preview", f"p4:distcancel:{job_id}")],
    ]
    _edit(
        update,
        actions,
        f"<b>Lead Distribution Preview</b>\nMethod: {job['method'].replace('_', ' ').title()}\nEligible leads: {job['selected_count']}\nProposed share:\n{distribution}\nFilters: {filters}\n\nAssignments are rechecked and committed in a bounded transaction.",
        InlineKeyboardMarkup(rows),
    )


def _performance(
    conn: Connection,
    update: Update,
    actions: list[dict[str, Any]],
    user_id: int,
    *,
    team_id: UUID | None = None,
    period: str | None = None,
    member_id: int | None = None,
) -> None:
    actor = team.get_user(conn, user_id)
    if (
        actor["role"] in {"SALES_MANAGER", "OBSERVER", "CUSTOM"}
        and team_id is None
        and member_id is None
    ):
        teams = team.list_teams(conn, user_id)
        rows = [[button(t["name"][:32], f"p4:perfteam:{t['id']}")] for t in teams]
        rows.append([button("Back", "p4:menu")])
        _edit(
            update,
            actions,
            "Choose an authorized team to view recorded performance for the last 30 days.",
            InlineKeyboardMarkup(rows),
        )
        return
    if period is None:
        periods = (
            ("TODAY", "Today"),
            ("YESTERDAY", "Yesterday"),
            ("LAST_7", "Last 7 days"),
            ("LAST_30", "Last 30 days"),
            ("THIS_MONTH", "This month"),
            ("PREVIOUS_MONTH", "Previous month"),
            ("CUSTOM", "Custom dates"),
        )
        if member_id is not None:
            rows = [
                [button(label, f"p4:perfmemberperiod:{member_id}:{key}")] for key, label in periods
            ]
        else:
            rows = [
                [button(label, f"p4:perfperiod:{team_id or 'all'}:{key}")] for key, label in periods
            ]
        rows.append([button("Team Workspace", "p4:menu")])
        _edit(
            update,
            actions,
            "Choose a date range for recorded team performance.",
            InlineKeyboardMarkup(rows),
        )
        return
    timezone_row = conn.execute(
        "select timezone from user_settings where telegram_user_id=%s", (user_id,)
    ).fetchone()
    zone = ZoneInfo(timezone_row["timezone"] if timezone_row else "Africa/Addis_Ababa")
    today = datetime.now(zone).date()
    end_day = today + timedelta(days=1)
    if period == "TODAY":
        start_day, label = today, "Today"
    elif period == "YESTERDAY":
        start_day, end_day, label = today - timedelta(days=1), today, "Yesterday"
    elif period == "LAST_7":
        start_day, label = today - timedelta(days=6), "Last 7 days"
    elif period == "LAST_30":
        start_day, label = today - timedelta(days=29), "Last 30 days"
    elif period == "THIS_MONTH":
        start_day, label = today.replace(day=1), "This month"
    elif period == "PREVIOUS_MONTH":
        end_day = today.replace(day=1)
        start_day = (end_day - timedelta(days=1)).replace(day=1)
        label = "Previous month"
    elif period == "CUSTOM":
        state = _state(conn, user_id)
        payload = (state or {}).get("payload") or {}
        if not state or state["purpose"] != "P4_PERFORMANCE_CUSTOM":
            raise ValueError("Custom performance range expired")
        start_day = date.fromisoformat(payload["start_date"])
        end_day = date.fromisoformat(payload["end_date"])
        member_id = int(payload["member_id"]) if payload.get("member_id") else None
        label = f"{start_day.isoformat()} to {(end_day - timedelta(days=1)).isoformat()}"
    else:
        raise ValueError("Choose a supported performance period")
    starts_at = datetime.combine(start_day, time.min, tzinfo=zone).astimezone(UTC)
    ends_at = datetime.combine(end_day, time.min, tzinfo=zone).astimezone(UTC)
    data = team.team_performance(
        conn, user_id, starts_at, ends_at, team_id=team_id, user_id=member_id
    )
    lines = [f"<b>Performance · {html.escape(label)}</b>"]
    if not data:
        lines.append("No salesperson activity is recorded for this period.")
    for row in data[:30]:
        calls = int(row["calls_logged"] or 0)
        answered = int(row["answered_calls"] or 0)
        rate = round(100 * answered / calls, 1) if calls else 0
        lines.append(
            f"\n<b>{html.escape(row['display_name'] or str(row['telegram_user_id']))}</b>\n"
            f"Calls {calls} · reached {row['unique_contacts_reached']} · answer rate {rate}%\n"
            f"Qualified {row['qualified_buyers']} · visits {row['visits_completed']} · "
            f"opportunities {row['opportunities_created']}\nVerified sales {row['verified_sales']} · "
            f"values {html.escape(json.dumps(row['verified_sales_by_currency'] or {}, ensure_ascii=False))}"
        )
    _edit(
        update,
        actions,
        "\n".join(lines),
        InlineKeyboardMarkup(
            [
                [
                    button(
                        "Change Date Range",
                        f"p4:memberperf:{member_id}"
                        if member_id
                        else f"p4:perfteam:{team_id}"
                        if team_id
                        else "p4:performance",
                    )
                ],
                [button("Team Workspace", "p4:menu")],
            ]
        ),
    )


def _targets(conn: Connection, update: Update, actions: list[dict[str, Any]], user_id: int) -> None:
    records = team.list_targets(conn, user_id)
    lines = ["<b>Targets</b>"]
    for row in records:
        target = float(row["target_amount"])
        actual = float(row["actual"] or 0)
        progress = min(1000, round(actual * 100 / target, 1)) if target > 0 else 0
        scope = row["display_name"] or row["team_name"] or "Campaign"
        lines.append(
            f"\n{html.escape(row['target_type'].replace('_', ' ').title())} · {html.escape(scope)}\n"
            f"{actual:g} / {target:g} {html.escape(row.get('currency') or '')} · {progress:g}%"
        )
    if len(lines) == 1:
        lines.append("\nNo active targets are set for your account or authorized teams.")
    rows = []
    if team.permission_allowed(conn, user_id, "targets.manage_team"):
        rows.append([button("Create Target", "p4:targetnew")])
    rows.append([button("Team Workspace", "p4:menu")])
    _edit(update, actions, "\n".join(lines), InlineKeyboardMarkup(rows))


def _campaigns(
    conn: Connection, update: Update, actions: list[dict[str, Any]], user_id: int
) -> None:
    records = team.campaign_performance(conn, user_id)
    rows = [
        [button(f"{row['name'][:25]} · {row['status'].title()}", f"p4:campaign:{row['id']}")]
        for row in records
    ]
    if team.permission_allowed(conn, user_id, "campaigns.manage"):
        rows.append([button("Create Campaign", "camp:new")])
    rows.append([button("Team Workspace", "p4:menu")])
    if records:
        _edit(
            update,
            actions,
            "<b>Calling Campaigns</b>\nSelect a campaign to view progress and controls.",
            InlineKeyboardMarkup(rows),
        )
    else:
        _edit(
            update,
            actions,
            "<b>Calling Campaigns</b>\nNo campaign is available in your authorized scope.",
            InlineKeyboardMarkup(rows),
        )


def _campaign_card(
    conn: Connection, update: Update, actions: list[dict[str, Any]], user_id: int, campaign_id: UUID
) -> None:
    record = next((row for row in team.campaign_performance(conn, user_id, campaign_id)), None)
    if not record:
        raise ValueError("Campaign is outside your authorized scope")
    text = (
        f"<b>{html.escape(record['name'])}</b> · {html.escape(record['status'].title())}\n"
        f"Source: {html.escape(record['source'] or 'Unspecified')}\n"
        f"Team: {html.escape(record['team_name'] or 'Not assigned')}\n"
        f"Calling window: {html.escape(str(record['starts_at'] or 'No start date')[:16])} – {html.escape(str(record['ends_at'] or 'No end date')[:16])}\n"
        f"Priority: {record['calling_priority']} · Max attempts: {record['max_call_attempts']} · Retry: {record['retry_after_hours']} hours · Target: {html.escape(str(record['performance_target'] if record['performance_target'] is not None else 'Not set'))}\n"
        f"Eligible leads: {record['total_eligible_leads']} · Assigned: {record['assigned_leads']}\n"
        f"Contacted: {record['leads_contacted']} · Calls logged: {record['calls_logged']}\n"
        f"Unique reached: {record['unique_contacts_reached']} · Qualified: {record['qualified_buyers']}\n"
        f"Follow-ups: {record['followups_scheduled']} · Visits: {record['visits_completed']}/{record['visits_scheduled']} scheduled\n"
        f"Opportunities: {record['opportunities_created']} · Verified sales: {record['verified_sales']}"
    )
    rows = []
    if team.permission_allowed(conn, user_id, "campaigns.manage"):
        rows.append([button("Assign Campaign Team", f"p4:campaignt:{campaign_id}")])
        rows.append([button("Edit Calling Rules", f"p4:campaignrules:{campaign_id}")])
    if record["status"] == "DRAFT" and team.permission_allowed(conn, user_id, "campaigns.manage"):
        rows.append([button("Mark Ready", f"p4:campaignstatus:{campaign_id}:READY")])
    if record["status"] == "READY" and team.permission_allowed(conn, user_id, "campaigns.manage"):
        rows.append([button("Start Campaign", f"p4:campaignstatus:{campaign_id}:ACTIVE")])
    if (
        record["team_name"]
        and record["status"] == "ACTIVE"
        and team.permission_allowed(conn, user_id, "leads.distribute")
    ):
        rows.append([button("Preview Lead Assignment", f"p4:campaignassign:{campaign_id}")])
    if record["status"] == "ACTIVE" and (
        team.permission_allowed(conn, user_id, "campaigns.manage")
        or team.permission_allowed(conn, user_id, "campaigns.team")
    ):
        rows.append([button("Pause Campaign", f"p4:campaignstatus:{campaign_id}:PAUSED")])
    elif record["status"] == "PAUSED" and (
        team.permission_allowed(conn, user_id, "campaigns.manage")
        or team.permission_allowed(conn, user_id, "campaigns.team")
    ):
        rows.append([button("Resume Campaign", f"p4:campaignstatus:{campaign_id}:ACTIVE")])
    if record["status"] in {"ACTIVE", "PAUSED"} and team.permission_allowed(
        conn, user_id, "campaigns.manage"
    ):
        rows.append([button("Complete Campaign", f"p4:campaignstatus:{campaign_id}:COMPLETED")])
        rows.append([button("Cancel Campaign", f"p4:campaignstatus:{campaign_id}:CANCELLED")])
    rows.extend(
        [[button("Calling Campaigns", "p4:campaigns")], [button("Team Workspace", "p4:menu")]]
    )
    _edit(update, actions, text, InlineKeyboardMarkup(rows))


def role_menu(
    conn: Connection, update: Update, actions: list[dict[str, Any]], user_id: int
) -> None:
    _menu(conn, update, actions, user_id)


def handle_invitation_start(
    conn: Connection, update: Update, user_id: int, token: str
) -> list[dict[str, Any]]:
    message = update.effective_message
    actions: list[dict[str, Any]] = []
    if not message:
        return actions
    try:
        user = team.accept_invitation(
            conn, token, user_id, update.effective_user.full_name if update.effective_user else ""
        )
        actions.append(
            {
                "type": "send",
                "chat_id": message.chat_id,
                "text": f"Welcome, {html.escape(user['display_name'] or 'team member')}. Your account is active as {user['role'].replace('_', ' ').title()}. Send /team to open your workspace.",
                "parse_mode": "HTML",
            }
        )
    except (PermissionError, ValueError) as exc:
        actions.append({"type": "send", "chat_id": message.chat_id, "text": str(exc)})
    return actions


def handle_callback(
    conn: Connection,
    update: Update,
    user_id: int,
    settings: Settings,
    update_id: int,
    actions: list[dict[str, Any]],
    data: str,
) -> bool:
    if not data.startswith("p4:"):
        return False
    parts = data.split(":")
    op = parts[1] if len(parts) > 1 else ""
    try:
        if op == "menu":
            _clear_state(conn, user_id)
            _menu(conn, update, actions, user_id)
        elif op == "invite":
            team.require_permission(conn, user_id, "users.manage")
            rows = [
                [button(role.replace("_", " ").title(), f"p4:inviterole:{role}")]
                for role in (
                    "SALES_MANAGER",
                    "SALESPERSON",
                    "MARKETING_ADMIN",
                    "OBSERVER",
                    "CUSTOM",
                )
            ]
            rows.append([button("Back", "p4:menu")])
            _edit(
                update,
                actions,
                "Choose the role to invite. Super administrator access can only be bootstrapped from the server environment.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "inviterole" and len(parts) == 3:
            _invite_role(conn, update, actions, user_id, parts[2])
        elif op == "inviteconfirm" and len(parts) == 4:
            role = parts[2]
            team_id = None if parts[3] == "none" else UUID(parts[3])
            if role in {"SALES_MANAGER", "MARKETING_ADMIN"}:
                _set_state(
                    conn,
                    user_id,
                    "P4_INVITE_CONFIRM",
                    payload={"role": role, "team_id": str(team_id) if team_id else None},
                )
                _edit(
                    update,
                    actions,
                    f"Confirm this {role.replace('_', ' ').title()} invitation. This role grants access to sensitive team workflows.",
                    InlineKeyboardMarkup(
                        [
                            [button("Confirm Role Invitation", "p4:managerinviteyes")],
                            [button("Cancel", "p4:invite")],
                        ]
                    ),
                )
            else:
                invite, token = team.create_invitation(conn, user_id, role, team_id=team_id)
                _edit(
                    update,
                    actions,
                    f"<b>One-time invitation created</b>\nRole: {html.escape(role.replace('_', ' ').title())}\nExpires: {html.escape(str(invite['expires_at']))}\nSend this to the invitee in a private Telegram chat:\n<code>/start {html.escape(token)}</code>",
                    InlineKeyboardMarkup([[button("Team Workspace", "p4:menu")]]),
                )
        elif op == "managerinviteyes":
            state = _state(conn, user_id)
            payload = (state or {}).get("payload") or {}
            if (
                not state
                or state["purpose"] != "P4_INVITE_CONFIRM"
                or payload.get("role") not in {"SALES_MANAGER", "MARKETING_ADMIN"}
            ):
                raise ValueError("Privileged role invitation confirmation expired")
            invite, token = team.create_invitation(
                conn,
                user_id,
                payload["role"],
                team_id=UUID(payload["team_id"]) if payload.get("team_id") else None,
            )
            _clear_state(conn, user_id)
            _edit(
                update,
                actions,
                f"<b>{html.escape(payload['role'].replace('_', ' ').title())} invitation created</b>\nExpires: {html.escape(str(invite['expires_at']))}\nSend this one-time code privately:\n<code>/start {html.escape(token)}</code>",
                InlineKeyboardMarkup([[button("Team Workspace", "p4:menu")]]),
            )
        elif op == "teams":
            _teams_page(conn, update, actions, user_id)
        elif op == "teamnew":
            team.require_permission(conn, user_id, "teams.manage")
            _set_state(conn, user_id, "P4_TEAM_NEW")
            _edit(
                update,
                actions,
                "Enter the new team name (up to 100 characters).",
                InlineKeyboardMarkup([[button("Cancel", "p4:teams")]]),
            )
        elif op == "teamrename" and len(parts) == 3:
            team.require_permission(conn, user_id, "teams.manage")
            team_id = UUID(parts[2])
            team_row = next((t for t in team.list_teams(conn, user_id) if t["id"] == team_id), None)
            if not team_row:
                raise ValueError("Team was not found")
            _set_state(conn, user_id, "P4_TEAM_RENAME", payload={"team_id": str(team_id)})
            _edit(
                update,
                actions,
                f"Enter a new name for <b>{html.escape(team_row['name'])}</b>.",
                InlineKeyboardMarkup([[button("Cancel", f"p4:team:{team_id}")]]),
            )
        elif op == "team" and len(parts) == 3:
            team_id = UUID(parts[2])
            row = next(
                (t for t in team.list_teams(conn, user_id) if str(t["id"]) == str(team_id)), None
            )
            if not row:
                raise ValueError("Team is no longer available")
            text = f"<b>{html.escape(row['name'])}</b>\nStatus: {'Active' if row['active'] else 'Inactive'}\nMembers: {row['member_count']}"
            rows = []
            if team.permission_allowed(conn, user_id, "teams.manage"):
                rows.append([button("Rename Team", f"p4:teamrename:{team_id}")])
                rows.append(
                    [
                        button(
                            "Deactivate Team" if row["active"] else "Activate Team",
                            f"p4:teamactive:{team_id}:{str(not row['active']).lower()}",
                        )
                    ]
                )
                if row["active"]:
                    rows.append([button("Set Team Manager", f"p4:teammanager:{team_id}")])
            rows.extend([[button("Teams", "p4:teams")], [button("Workspace", "p4:menu")]])
            _edit(update, actions, text, InlineKeyboardMarkup(rows))
        elif op == "teammanager" and len(parts) == 3:
            team.require_permission(conn, user_id, "teams.manage")
            members = [
                m
                for m in team.list_members(conn, user_id, 100)
                if m["role"] == "SALES_MANAGER" and m["account_status"] == "ACTIVE"
            ]
            rows = [
                [
                    button(
                        str(m["display_name"] or m["telegram_user_id"])[:35],
                        f"p4:teammanagerconfirm:{parts[2]}:{m['telegram_user_id']}",
                    )
                ]
                for m in members
            ]
            rows.append([button("Clear Manager", f"p4:teammanagerconfirm:{parts[2]}:none")])
            rows.append([button("Back", f"p4:team:{parts[2]}")])
            _edit(update, actions, "Choose the manager for this team.", InlineKeyboardMarkup(rows))
        elif op == "teammanagerconfirm" and len(parts) == 4:
            manager_id = None if parts[3] == "none" else int(parts[3])
            team.set_team_manager(conn, user_id, UUID(parts[2]), manager_id)
            _teams_page(conn, update, actions, user_id)
        elif op == "teamdisable" and len(parts) == 3:
            team.set_team_active(conn, user_id, UUID(parts[2]), False)
            _teams_page(conn, update, actions, user_id)
        elif op == "teamactive" and len(parts) == 4:
            team.set_team_active(conn, user_id, UUID(parts[2]), parts[3] == "true")
            _teams_page(conn, update, actions, user_id)
        elif op == "memberteams" and len(parts) == 3:
            team.require_permission(conn, user_id, "users.manage")
            member_id = int(parts[2])
            member = team.get_user(conn, member_id)
            if not member or member["role"] == "SUPER_ADMIN":
                raise ValueError("Choose a regular CRM member")
            rows = [
                [button(t["name"][:32], f"p4:memberteam:{member_id}:{t['id']}")]
                for t in team.list_teams(conn, user_id)
                if t["active"]
            ]
            rows.append([button("Back", f"p4:member:{member_id}")])
            _edit(
                update,
                actions,
                "Choose an active team for this member.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "memberteam" and len(parts) == 4:
            team.add_team_member(conn, user_id, UUID(parts[3]), int(parts[2]))
            _member_card(conn, update, actions, user_id, int(parts[2]))
        elif op == "audit":
            _audit_page(conn, update, actions, user_id)
        elif op == "settings":
            _team_settings_page(conn, update, actions, user_id)
        elif op == "setting" and len(parts) == 3:
            team.require_permission(conn, user_id, "users.manage")
            key = parts[2]
            current = team.get_team_settings(conn, user_id)
            if key not in {
                "unassigned_visible_to_salespeople",
                "salespeople_can_request_reassignment",
                "manager_can_override_ownership",
                "active_negotiation_requires_super_admin",
            }:
                raise ValueError("Setting is not supported")
            team.update_team_settings(conn, user_id, {key: not current[key]})
            _team_settings_page(conn, update, actions, user_id)
        elif op == "settingcapacity":
            team.require_permission(conn, user_id, "users.manage")
            _set_state(conn, user_id, "P4_SETTING_CAPACITY")
            _edit(
                update,
                actions,
                "Enter the maximum active leads per salesperson (1–100000).",
                InlineKeyboardMarkup([[button("Cancel", "p4:settings")]]),
            )
        elif op == "settingmethod":
            team.require_permission(conn, user_id, "users.manage")
            current = team.get_team_settings(conn, user_id)["default_distribution_method"]
            options = ("ROUND_ROBIN", "LOWEST_WORKLOAD", "MANUAL")
            selected = options[(options.index(current) + 1) % len(options)]
            team.update_team_settings(conn, user_id, {"default_distribution_method": selected})
            _team_settings_page(conn, update, actions, user_id)
        elif op == "members" and len(parts) == 3:
            _members_page(conn, update, actions, user_id, int(parts[2]))
        elif op == "invitations":
            _invitations_page(conn, update, actions, user_id)
        elif op == "inviterevoke" and len(parts) == 3:
            team.revoke_invitation(conn, user_id, UUID(parts[2]))
            _invitations_page(conn, update, actions, user_id)
        elif op == "member" and len(parts) == 3:
            _member_card(conn, update, actions, user_id, int(parts[2]))
        elif op == "rolechange" and len(parts) == 3:
            team.require_permission(conn, user_id, "users.manage")
            member_id = int(parts[2])
            rows = [
                [button(role.replace("_", " ").title(), f"p4:roleconfirm:{member_id}:{role}")]
                for role in (
                    "SALES_MANAGER",
                    "SALESPERSON",
                    "MARKETING_ADMIN",
                    "OBSERVER",
                    "CUSTOM",
                )
            ]
            rows.append([button("Back", f"p4:member:{member_id}")])
            _edit(
                update,
                actions,
                "Choose a new role. You cannot assign super administrator access here.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "roleconfirm" and len(parts) == 4:
            team.require_permission(conn, user_id, "users.manage")
            _set_state(
                conn,
                user_id,
                "P4_ROLE_CONFIRM",
                payload={"member_id": int(parts[2]), "role": parts[3]},
            )
            _edit(
                update,
                actions,
                f"Confirm changing this member's role to {html.escape(parts[3].replace('_', ' ').title())}.",
                InlineKeyboardMarkup(
                    [
                        [button("Confirm Role Change", "p4:roleyes")],
                        [button("Cancel", f"p4:member:{parts[2]}")],
                    ]
                ),
            )
        elif op == "roleyes":
            state = _state(conn, user_id)
            payload = (state or {}).get("payload") or {}
            if not state or state["purpose"] != "P4_ROLE_CONFIRM":
                raise ValueError("Role confirmation expired")
            team.change_role(conn, user_id, int(payload["member_id"]), payload["role"])
            _clear_state(conn, user_id)
            _member_card(conn, update, actions, user_id, int(payload["member_id"]))
        elif op == "suspend" and len(parts) == 3:
            team.require_permission(conn, user_id, "users.manage")
            member_id = int(parts[2])
            _edit(
                update,
                actions,
                "Suspending disables CRM access. Choose what to do with the member's assigned active leads.",
                InlineKeyboardMarkup(
                    [
                        [button("Keep Assigned Temporarily", f"p4:suspendkeep:{member_id}")],
                        [button("Transfer Leads First", f"p4:suspendtransfer:{member_id}")],
                        [button("Cancel", f"p4:member:{member_id}")],
                    ]
                ),
            )
        elif op == "suspendkeep" and len(parts) == 3:
            team.set_member_status(conn, user_id, int(parts[2]), "SUSPENDED", lead_handling="KEEP")
            _member_card(conn, update, actions, user_id, int(parts[2]))
        elif op == "suspendtransfer" and len(parts) == 3:
            team.require_permission(conn, user_id, "users.manage")
            members = [
                m
                for m in team.list_members(conn, user_id, 100)
                if m["role"] == "SALESPERSON"
                and m["account_status"] == "ACTIVE"
                and int(m["telegram_user_id"]) != int(parts[2])
            ]
            rows = [
                [
                    button(
                        str(m["display_name"] or m["telegram_user_id"])[:35],
                        f"p4:suspendto:{parts[2]}:{m['telegram_user_id']}",
                    )
                ]
                for m in members[:25]
            ]
            rows.append([button("Cancel", f"p4:member:{parts[2]}")])
            _edit(
                update,
                actions,
                "Select the salesperson to receive active opportunities. Transfer is bounded to 500 leads per confirmation.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "suspendto" and len(parts) == 4:
            team.set_member_status(
                conn,
                user_id,
                int(parts[2]),
                "SUSPENDED",
                lead_handling="TRANSFER",
                transfer_to=int(parts[3]),
            )
            _member_card(conn, update, actions, user_id, int(parts[2]))
        elif op == "reactivate" and len(parts) == 3:
            team.set_member_status(conn, user_id, int(parts[2]), "ACTIVE")
            _member_card(conn, update, actions, user_id, int(parts[2]))
        elif op == "handover":
            team.require_permission(conn, user_id, "handover.request")
            _set_state(conn, user_id, "P4_HANDOVER_CONTACT")
            _edit(
                update,
                actions,
                "Send the name or normalized phone number of the lead assigned to you.",
                InlineKeyboardMarkup([[button("Cancel", "p4:menu")]]),
            )
        elif op == "handovercontact" and len(parts) == 3:
            contact_id = UUID(parts[2])
            team.require_contact_access(conn, user_id, contact_id)
            contact = crm.get_contact(conn, contact_id)
            if not contact or contact.get("assigned_telegram_user_id") != user_id:
                raise PermissionError("You can request a handover only for a lead assigned to you")
            reasons = {
                "UNAVAILABLE": "Salesperson unavailable",
                "LEAVING_TEAM": "Leaving the team",
                "SPECIALIST": "Specialist needed",
                "WORKLOAD": "Workload redistribution",
                "CUSTOMER_REQUEST": "Customer requested a change",
                "OTHER": "Other reason",
            }
            rows = [
                [button(label, f"p4:handreason:{contact_id}:{code}")]
                for code, label in reasons.items()
            ]
            rows.append([button("Cancel", "p4:menu")])
            _edit(
                update,
                actions,
                f"Why should <b>{html.escape(contact['name'])}</b> be handed over?",
                InlineKeyboardMarkup(rows),
            )
        elif op == "handreason" and len(parts) == 4:
            contact_id, reason = UUID(parts[2]), parts[3]
            team.require_contact_access(conn, user_id, contact_id)
            contact = crm.get_contact(conn, contact_id)
            if not contact or contact.get("assigned_telegram_user_id") != user_id:
                raise PermissionError("Lead is no longer assigned to your account")
            snapshot = conn.execute(
                """select (select count(*) from follow_ups f where f.contact_id=c.id and f.status='PENDING') followups,
                          (select count(*) from sales_opportunities o where o.contact_id=c.id and o.final_outcome is null) opportunities,
                          (select count(*) from property_visits v where v.contact_id=c.id and v.visit_status in ('SCHEDULED','BUYER_ARRIVED')) visits,
                          (select count(*) from negotiation_records n join sales_opportunities o on o.id=n.opportunity_id
                            where o.contact_id=c.id and o.final_outcome is null and n.negotiation_status not in ('REJECTED','EXPIRED','CANCELLED')) negotiations,
                          (select count(*) from reservations r join sales_opportunities o on o.id=r.opportunity_id
                            where o.contact_id=c.id and r.reservation_status not in ('CANCELLED','EXPIRED')) reservations
                   from contacts c where c.id=%s""",
                (contact_id,),
            ).fetchone()
            _set_state(
                conn,
                user_id,
                "P4_HANDOVER_CONFIRM",
                payload={"contact_id": str(contact_id), "reason": reason},
            )
            _edit(
                update,
                actions,
                f"<b>Handover preview · {html.escape(contact['name'])}</b>\nReason: {html.escape(reason.replace('_', ' ').title())}\nPending follow-ups: {snapshot['followups']} · Active opportunities: {snapshot['opportunities']}\nScheduled visits: {snapshot['visits']} · Negotiations: {snapshot['negotiations']} · Reservations: {snapshot['reservations']}\n\nA manager will review and choose the receiving salesperson. Existing CRM history and tasks are preserved.",
                InlineKeyboardMarkup(
                    [[button("Request Handover", "p4:handoveryes")], [button("Cancel", "p4:menu")]]
                ),
            )
        elif op == "handoveryes":
            state = _state(conn, user_id)
            payload = (state or {}).get("payload") or {}
            if not state or state["purpose"] != "P4_HANDOVER_CONFIRM":
                raise ValueError("Handover confirmation expired")
            request = team.create_handover_request(
                conn, user_id, UUID(payload["contact_id"]), payload["reason"]
            )
            _clear_state(conn, user_id)
            recipients = {
                int(row["telegram_user_id"])
                for row in conn.execute(
                    """select distinct t.manager_telegram_user_id telegram_user_id
                       from crm_team_memberships mine join crm_teams t on t.id=mine.team_id and t.active
                       join crm_users manager on manager.telegram_user_id=t.manager_telegram_user_id
                         and manager.account_status='ACTIVE'
                       where mine.telegram_user_id=%s and mine.active
                         and t.manager_telegram_user_id is not null""",
                    (user_id,),
                ).fetchall()
            }
            snapshot = request["task_snapshot"] or {}
            active_deal = bool(
                snapshot.get("active_negotiations", 0) or snapshot.get("active_reservations", 0)
            )
            needs_super_admin = False
            if active_deal:
                policy = conn.execute(
                    """select active_negotiation_requires_super_admin,manager_can_override_ownership
                       from crm_team_settings where singleton"""
                ).fetchone()
                needs_super_admin = bool(
                    not policy
                    or policy["active_negotiation_requires_super_admin"]
                    or not policy["manager_can_override_ownership"]
                )
                if needs_super_admin:
                    recipients.update(
                        int(row["telegram_user_id"])
                        for row in conn.execute(
                            """select telegram_user_id from crm_users
                               where role='SUPER_ADMIN' and account_status='ACTIVE'"""
                        ).fetchall()
                    )
            for recipient_id in recipients - {user_id}:
                actions.append(
                    {
                        "type": "send",
                        "chat_id": recipient_id,
                        "text": (
                            "A salesperson requested a lead handover. Super-admin approval is required "
                            "because the lead has an active negotiation or reservation. Open Team Management → Handover Requests."
                            if needs_super_admin
                            else "A salesperson requested a lead handover. Open Team Management → Handover Requests."
                        ),
                        "markup": InlineKeyboardMarkup(
                            [[button("Review Request", "p4:handovers")]]
                        ),
                    }
                )
            _edit(
                update,
                actions,
                f"Handover request recorded ({request['id']}). Your manager can review the lead and active tasks.",
                InlineKeyboardMarkup([[button("Workspace", "p4:menu")]]),
            )
        elif op == "handovers":
            _handovers_page(conn, update, actions, user_id)
        elif op == "handoverrequest" and len(parts) == 3:
            request_id = UUID(parts[2])
            request = next(
                (r for r in team.list_handover_requests(conn, user_id) if r["id"] == request_id),
                None,
            )
            if not request:
                raise ValueError("Handover request is outside your scope or no longer pending")
            snap = request["task_snapshot"] or {}
            actor = team.get_user(conn, user_id)
            policy = conn.execute(
                """select active_negotiation_requires_super_admin,manager_can_override_ownership
                   from crm_team_settings where singleton"""
            ).fetchone()
            active_deal = bool(
                snap.get("active_negotiations", 0) or snap.get("active_reservations", 0)
            )
            needs_super_admin = bool(
                active_deal
                and (
                    not policy
                    or policy["active_negotiation_requires_super_admin"]
                    or not policy["manager_can_override_ownership"]
                )
            )
            members = [
                m
                for m in team.list_members(conn, user_id, 100)
                if m["role"] == "SALESPERSON"
                and m["account_status"] == "ACTIVE"
                and m["can_receive_leads"]
                and int(m["telegram_user_id"]) != request["current_owner"]
            ]
            if needs_super_admin and actor["role"] != "SUPER_ADMIN":
                members = []
            rows = [
                [
                    button(
                        str(m["display_name"] or m["telegram_user_id"])[:32],
                        f"p4:handoverto:{request_id}:{m['telegram_user_id']}",
                    )
                ]
                for m in members[:25]
            ]
            rows.extend(
                [
                    [button("Reject Request", f"p4:handoverno:{request_id}")],
                    [button("Back", "p4:handovers")],
                ]
            )
            review_note = (
                "\nActive negotiation or reservation: a super-admin must approve reassignment."
                if needs_super_admin and actor["role"] != "SUPER_ADMIN"
                else ""
            )
            _edit(
                update,
                actions,
                f"<b>{html.escape(request['contact_name'])}</b> · {html.escape(request['reason'].replace('_', ' ').title())}\nRequested by: {html.escape(request['requester_name'] or str(request['requested_by']))}\nPending follow-ups: {snap.get('pending_followups', 0)} · Active opportunities: {snap.get('active_opportunities', 0)}\nVisits: {snap.get('upcoming_visits', 0)} · Negotiations: {snap.get('active_negotiations', 0)} · Reservations: {snap.get('active_reservations', 0)}{review_note}\nChoose a receiving salesperson or reject the request.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "handoverto" and len(parts) == 4:
            request_id, owner_id = UUID(parts[2]), int(parts[3])
            request = next(
                (r for r in team.list_handover_requests(conn, user_id) if r["id"] == request_id),
                None,
            )
            if not request:
                raise ValueError("Handover request is no longer pending")
            team.require_user_in_scope(conn, user_id, owner_id)
            owner = team.get_user(conn, owner_id)
            snap = request["task_snapshot"] or {}
            _edit(
                update,
                actions,
                f"Confirm handover of <b>{html.escape(request['contact_name'])}</b> to <b>{html.escape(owner['display_name'] or str(owner_id))}</b>?\nReason: {html.escape(request['reason'].replace('_', ' ').title())}\nPending follow-ups: {snap.get('pending_followups', 0)} · Opportunities: {snap.get('active_opportunities', 0)} · Visits: {snap.get('upcoming_visits', 0)} · Negotiations: {snap.get('active_negotiations', 0)} · Reservations: {snap.get('active_reservations', 0)}",
                InlineKeyboardMarkup(
                    [
                        [button("Confirm Handover", f"p4:handoversyes:{request_id}:{owner_id}")],
                        [button("Back", f"p4:handoverrequest:{request_id}")],
                    ]
                ),
            )
        elif op == "handoversyes" and len(parts) == 4:
            request_id, owner_id = UUID(parts[2]), int(parts[3])
            request = team.review_handover_request(
                conn, user_id, request_id, approve=True, new_owner_id=owner_id
            )
            contact = crm.get_contact(conn, request["contact_id"])
            for recipient in {request["current_owner"], owner_id} - {None, user_id}:
                actions.append(
                    {
                        "type": "send",
                        "chat_id": recipient,
                        "text": f"Lead handover completed for {contact['name'] if contact else 'a lead'}. Existing CRM history and tasks remain available.",
                    }
                )
            _handovers_page(conn, update, actions, user_id)
        elif op == "handoverno" and len(parts) == 3:
            team.review_handover_request(conn, user_id, UUID(parts[2]), approve=False)
            _handovers_page(conn, update, actions, user_id)
        elif op == "assign":
            team.require_permission(conn, user_id, "leads.assign")
            _set_state(conn, user_id, "P4_ASSIGN_CONTACT")
            _edit(
                update,
                actions,
                "Send a buyer name or normalized phone number to search for a lead to assign.",
                InlineKeyboardMarkup([[button("Cancel", "p4:menu")]]),
            )
        elif op == "assigncontact" and len(parts) == 3:
            team.require_permission(conn, user_id, "leads.assign")
            contact_id = UUID(parts[2])
            team.require_contact_access(conn, user_id, contact_id)
            contact = crm.get_contact(conn, contact_id)
            if not contact:
                raise ValueError("Lead is no longer available")
            reasons = {
                "WORKLOAD": "Workload redistribution",
                "SPECIALIST": "Specialist needed",
                "CUSTOMER_REQUEST": "Customer requested a change",
                "MANAGER_INTERVENTION": "Manager intervention",
                "OTHER": "Other reason",
            }
            rows = [
                [button(label, f"p4:assignreason:{contact_id}:{reason}")]
                for reason, label in reasons.items()
            ]
            rows.append([button("Cancel", "p4:menu")])
            _set_state(
                conn,
                user_id,
                "P4_ASSIGN_REASON",
                contact_id=contact_id,
                payload={"contact_id": str(contact_id)},
            )
            _edit(
                update,
                actions,
                f"Choose a reason for assigning <b>{html.escape(contact['name'])}</b>.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "assignreason" and len(parts) == 4:
            team.require_permission(conn, user_id, "leads.assign")
            contact_id, reason = UUID(parts[2]), parts[3]
            state = _state(conn, user_id)
            payload = (state or {}).get("payload") or {}
            if (
                not state
                or state["purpose"] != "P4_ASSIGN_REASON"
                or payload.get("contact_id") != str(contact_id)
            ):
                raise ValueError("Assignment selection expired")
            team.require_contact_access(conn, user_id, contact_id)
            if reason not in {
                "WORKLOAD",
                "SPECIALIST",
                "CUSTOMER_REQUEST",
                "MANAGER_INTERVENTION",
                "OTHER",
            }:
                raise ValueError("Choose a supported assignment reason")
            contact = crm.get_contact(conn, contact_id)
            if not contact:
                raise ValueError("Lead is no longer available")
            members = [
                m
                for m in team.list_members(conn, user_id, 100)
                if m["role"] == "SALESPERSON"
                and m["account_status"] == "ACTIVE"
                and m["can_receive_leads"]
            ]
            rows = [
                [
                    button(
                        str(m["display_name"] or m["telegram_user_id"])[:34],
                        f"p4:assignowner:{contact_id}:{m['telegram_user_id']}",
                    )
                ]
                for m in members
            ]
            rows.append([button("Cancel", "p4:menu")])
            _set_state(
                conn,
                user_id,
                "P4_ASSIGN_OWNER",
                contact_id=contact_id,
                payload={
                    "contact_id": str(contact_id),
                    "old_owner": contact.get("assigned_telegram_user_id"),
                    "reason": reason,
                },
            )
            _edit(
                update,
                actions,
                f"Choose a receiving salesperson for <b>{html.escape(contact['name'])}</b>. Reason: {html.escape(reason.replace('_', ' ').title())}.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "assignowner" and len(parts) == 4:
            state = _state(conn, user_id)
            payload = (state or {}).get("payload") or {}
            contact_id = UUID(parts[2])
            if (
                not state
                or state["purpose"] != "P4_ASSIGN_OWNER"
                or payload.get("contact_id") != str(contact_id)
            ):
                raise ValueError("Lead assignment selection expired")
            team.require_contact_access(conn, user_id, contact_id)
            owner = team.get_user(conn, int(parts[3]))
            if (
                not owner
                or owner["role"] != "SALESPERSON"
                or owner["account_status"] != "ACTIVE"
                or not owner["can_receive_leads"]
            ):
                raise ValueError("Receiving salesperson is no longer eligible")
            _set_state(
                conn,
                user_id,
                "P4_ASSIGN_CONFIRM",
                contact_id=contact_id,
                payload=payload | {"new_owner": int(parts[3])},
            )
            snapshot = conn.execute(
                """select (select count(*) from follow_ups f where f.contact_id=c.id and f.status='PENDING') followups,
                          (select count(*) from sales_opportunities o where o.contact_id=c.id and o.final_outcome is null) opportunities,
                          (select count(*) from property_visits v where v.contact_id=c.id and v.visit_status in ('SCHEDULED','BUYER_ARRIVED')) visits,
                          (select count(*) from negotiation_records n join sales_opportunities o on o.id=n.opportunity_id where o.contact_id=c.id and o.final_outcome is null and n.negotiation_status not in ('REJECTED','EXPIRED','CANCELLED')) negotiations,
                          (select count(*) from reservations r join sales_opportunities o on o.id=r.opportunity_id where o.contact_id=c.id and r.reservation_status not in ('CANCELLED','EXPIRED')) reservations
                   from contacts c where c.id=%s""",
                (contact_id,),
            ).fetchone()
            _edit(
                update,
                actions,
                f"Confirm assigning this lead to <b>{html.escape(owner['display_name'] or str(owner['telegram_user_id']))}</b>?\nReason: {html.escape(payload['reason'].replace('_', ' ').title())}\nPending follow-ups: {snapshot['followups']} · Active opportunities: {snapshot['opportunities']}\nVisits: {snapshot['visits']} · Negotiations: {snapshot['negotiations']} · Reservations: {snapshot['reservations']}\nAll history and existing tasks stay attached to the lead.",
                InlineKeyboardMarkup(
                    [[button("Confirm Assignment", "p4:assignyes")], [button("Cancel", "p4:menu")]]
                ),
            )
        elif op == "assignyes":
            state = _state(conn, user_id)
            payload = (state or {}).get("payload") or {}
            if not state or state["purpose"] != "P4_ASSIGN_CONFIRM":
                raise ValueError("Assignment confirmation expired")
            contact_id = UUID(payload["contact_id"])
            history = team.assign_contact(
                conn,
                user_id,
                contact_id,
                int(payload["new_owner"]),
                reason=payload["reason"].replace("_", " ").title(),
                source="MANUAL",
                idempotency_key=f"telegram:{user_id}:{update_id}",
                override_active_deal=(team.get_user(conn, user_id)["role"] == "SUPER_ADMIN"),
                override_capacity=(team.get_user(conn, user_id)["role"] == "SUPER_ADMIN"),
            )
            _clear_state(conn, user_id)
            new_owner = int(payload["new_owner"])
            actions.append(
                {
                    "type": "send",
                    "chat_id": new_owner,
                    "text": "A new lead was assigned to you. Open My Leads to review it.",
                }
            )
            old_owner = history.get("previous_owner_telegram_user_id")
            if old_owner and old_owner != new_owner and old_owner != user_id:
                actions.append(
                    {
                        "type": "send",
                        "chat_id": old_owner,
                        "text": "A lead was reassigned by an authorized manager. Its recorded history and follow-ups remain in the CRM.",
                    }
                )
            _edit(
                update,
                actions,
                "Lead assignment recorded in the assignment history and audit log.",
                InlineKeyboardMarkup([[button("Team Workspace", "p4:menu")]]),
            )
        elif op == "dist":
            team.require_permission(conn, user_id, "leads.distribute")
            teams = team.list_teams(conn, user_id)
            rows = [
                [button(t["name"][:32], f"p4:distteam:{t['id']}")] for t in teams if t["active"]
            ]
            rows.append([button("Cancel", "p4:menu")])
            _edit(
                update,
                actions,
                "Choose the team that will receive unassigned eligible leads.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "distteam" and len(parts) == 3:
            team_id = UUID(parts[2])
            rows = [
                [button("Round Robin", f"p4:distmethod:{team_id}:ROUND_ROBIN")],
                [button("Lowest Workload", f"p4:distmethod:{team_id}:LOWEST_WORKLOAD")],
                [button("Back", "p4:dist")],
            ]
            _edit(
                update,
                actions,
                "Choose a transparent distribution method. Preview is limited to 500 leads.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "distmethod" and len(parts) == 4:
            team.require_permission(conn, user_id, "leads.distribute")
            _set_state(
                conn,
                user_id,
                "P4_DIST_FILTER_CHOICE",
                payload={"team_id": parts[2], "method": parts[3]},
            )
            _edit(
                update,
                actions,
                "Choose all eligible unassigned leads or add filters. Filter values are checked by the server.",
                InlineKeyboardMarkup(
                    [
                        [button("All Eligible Leads", "p4:distall")],
                        [button("Set Filters", "p4:distfilter")],
                        [button("Cancel", "p4:menu")],
                    ]
                ),
            )
        elif op == "distall":
            state = _state(conn, user_id)
            payload = (state or {}).get("payload") or {}
            if not state or state["purpose"] != "P4_DIST_FILTER_CHOICE":
                raise ValueError("Distribution selection expired")
            job = team.preview_distribution(
                conn,
                user_id,
                UUID(payload["team_id"]),
                method=payload["method"],
                idempotency_key=f"telegram:{user_id}:{update_id}",
            )
            _clear_state(conn, user_id)
            _distribution_preview(conn, update, actions, job["id"])
        elif op == "distfilter":
            state = _state(conn, user_id)
            payload = (state or {}).get("payload") or {}
            if not state or state["purpose"] != "P4_DIST_FILTER_CHOICE":
                raise ValueError("Distribution selection expired")
            _set_state(conn, user_id, "P4_DIST_FILTERS", payload=payload)
            _edit(
                update,
                actions,
                "Enter comma-separated key=value filters. Supported keys: import_batch_id, campaign_id, lead_status, interest_level, lead_source, priority_category, qualification_status, property_type, location, min_budget, max_budget, created_after, created_before. Example: lead_status=NEW,priority_category=A,location=Bole",
                InlineKeyboardMarkup([[button("Cancel", "p4:menu")]]),
            )
        elif op == "distconfirm" and len(parts) == 3:
            result = team.apply_distribution(conn, user_id, UUID(parts[2]))
            owners = conn.execute(
                """select new_owner_telegram_user_id owner_id,count(*) amount
                   from lead_assignment_history
                   where decision_facts->>'job_id'=%s and new_owner_telegram_user_id is not null
                   group by new_owner_telegram_user_id""",
                (str(UUID(parts[2])),),
            ).fetchall()
            for owner in owners:
                if owner["owner_id"] != user_id:
                    actions.append(
                        {
                            "type": "send",
                            "chat_id": owner["owner_id"],
                            "text": f"{owner['amount']} new lead(s) were assigned to your calling queue.",
                        }
                    )
            _edit(
                update,
                actions,
                f"Assignment complete. {result['applied_count']} of {result['selected_count']} leads assigned.",
                InlineKeyboardMarkup([[button("Team Workspace", "p4:menu")]]),
            )
        elif op == "distcancel" and len(parts) == 3:
            team.cancel_distribution(conn, user_id, UUID(parts[2]))
            _menu(conn, update, actions, user_id)
        elif op == "performance":
            if team.get_user(conn, user_id)["role"] == "MARKETING_ADMIN":
                _campaigns(conn, update, actions, user_id)
            else:
                _performance(conn, update, actions, user_id, period=None)
        elif op == "perfteam" and len(parts) == 3:
            _performance(conn, update, actions, user_id, team_id=UUID(parts[2]), period=None)
        elif op == "memberactivity" and len(parts) == 3:
            _audit_page(conn, update, actions, user_id, subject_user_id=int(parts[2]))
        elif op == "memberperf" and len(parts) == 3:
            _performance(conn, update, actions, user_id, member_id=int(parts[2]), period=None)
        elif op == "perfperiod" and len(parts) == 4:
            team_id = None if parts[2] == "all" else UUID(parts[2])
            if parts[3] == "CUSTOM":
                _set_state(
                    conn,
                    user_id,
                    "P4_PERFORMANCE_CUSTOM",
                    payload={"team_id": str(team_id) if team_id else None},
                )
                _edit(
                    update,
                    actions,
                    "Enter start and end dates as YYYY-MM-DD, YYYY-MM-DD. The end date is inclusive.",
                    InlineKeyboardMarkup([[button("Cancel", "p4:performance")]]),
                )
            else:
                _performance(conn, update, actions, user_id, team_id=team_id, period=parts[3])
        elif op == "perfmemberperiod" and len(parts) == 4:
            member_id, period = int(parts[2]), parts[3]
            if period == "CUSTOM":
                _set_state(conn, user_id, "P4_PERFORMANCE_CUSTOM", payload={"member_id": member_id})
                _edit(
                    update,
                    actions,
                    "Enter start and end dates as YYYY-MM-DD, YYYY-MM-DD. The end date is inclusive.",
                    InlineKeyboardMarkup([[button("Cancel", f"p4:memberperf:{member_id}")]]),
                )
            else:
                _performance(conn, update, actions, user_id, member_id=member_id, period=period)
        elif op == "campaigns":
            _campaigns(conn, update, actions, user_id)
        elif op == "campaign" and len(parts) == 3:
            _campaign_card(conn, update, actions, user_id, UUID(parts[2]))
        elif op == "campaignrules" and len(parts) == 3:
            team.require_permission(conn, user_id, "campaigns.manage")
            campaign_id = UUID(parts[2])
            team.require_campaign_scope(conn, user_id, campaign_id)
            _set_state(
                conn, user_id, "P4_CAMPAIGN_RULES", payload={"campaign_id": str(campaign_id)}
            )
            _edit(
                update,
                actions,
                "Enter six fields separated by |: start date or -, end date or -, priority 0–100, max attempts 1–50, retry hours 0–720, performance target or -. Dates use YYYY-MM-DD and the end date is inclusive. Example: 2026-11-01 | 2026-11-30 | 80 | 8 | 24 | 5",
                InlineKeyboardMarkup([[button("Cancel", f"p4:campaign:{campaign_id}")]]),
            )
        elif op == "campaignt" and len(parts) == 3:
            team.require_permission(conn, user_id, "campaigns.manage")
            campaign_id = UUID(parts[2])
            team.require_campaign_scope(conn, user_id, campaign_id)
            rows = [
                [button(t["name"][:32], f"p4:campaigntset:{campaign_id}:{t['id']}")]
                for t in team.list_teams(conn, user_id)
                if t["active"]
            ]
            rows.append([button("Back", f"p4:campaign:{campaign_id}")])
            _edit(
                update,
                actions,
                "Choose the campaign team. Its active salespeople will become campaign participants.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "campaigntset" and len(parts) == 4:
            campaign_id = UUID(parts[2])
            team.set_campaign_team(conn, user_id, campaign_id, UUID(parts[3]))
            _campaign_card(conn, update, actions, user_id, campaign_id)
        elif op == "campaignassign" and len(parts) == 3:
            campaign_id = UUID(parts[2])
            campaign = team.require_campaign_scope(conn, user_id, campaign_id)
            if not campaign["team_id"]:
                raise ValueError("Assign this campaign to a team before distributing leads")
            job = team.preview_distribution(
                conn,
                user_id,
                campaign["team_id"],
                method="ROUND_ROBIN",
                filters={"campaign_id": str(campaign_id)},
                idempotency_key=f"telegram:{user_id}:{update_id}",
            )
            _distribution_preview(conn, update, actions, job["id"])
        elif op == "campaignstatus" and len(parts) == 4:
            team.set_campaign_status(conn, user_id, UUID(parts[2]), parts[3])
            _campaign_card(conn, update, actions, user_id, UUID(parts[2]))
        elif op == "targets":
            state = _state(conn, user_id)
            if state and str(state["purpose"]).startswith("P4_TARGET_"):
                _clear_state(conn, user_id)
            _targets(conn, update, actions, user_id)
        elif op == "targetnew":
            team.require_permission(conn, user_id, "targets.manage_team")
            target_types = (
                "CALLS_LOGGED",
                "UNIQUE_CONTACTS_REACHED",
                "QUALIFIED_BUYERS",
                "FOLLOWUPS_COMPLETED",
                "VISITS_COMPLETED",
                "OPPORTUNITIES_CREATED",
                "RESERVATIONS_RECORDED",
                "VERIFIED_SALES_COUNT",
                "VERIFIED_SALES_VALUE",
            )
            rows = [
                [button(value.replace("_", " ").title(), f"p4:targettype:{value}")]
                for value in target_types
            ]
            rows.append([button("Cancel", "p4:targets")])
            _edit(
                update,
                actions,
                "Choose a target measured from recorded CRM activity. Verified sales value defaults to ETB in this form.",
                InlineKeyboardMarkup(rows),
            )
        elif op == "targettype" and len(parts) == 3:
            team.require_permission(conn, user_id, "targets.manage_team")
            target_type = parts[2].upper()
            if target_type not in {
                "CALLS_LOGGED",
                "UNIQUE_CONTACTS_REACHED",
                "QUALIFIED_BUYERS",
                "FOLLOWUPS_COMPLETED",
                "VISITS_COMPLETED",
                "OPPORTUNITIES_CREATED",
                "RESERVATIONS_RECORDED",
                "VERIFIED_SALES_COUNT",
                "VERIFIED_SALES_VALUE",
            }:
                raise ValueError("Target type is not supported")
            _set_state(conn, user_id, "P4_TARGET_AMOUNT", payload={"target_type": target_type})
            _edit(
                update,
                actions,
                "Enter the target amount as a nonnegative number.",
                InlineKeyboardMarkup([[button("Cancel", "p4:targets")]]),
            )
        elif op == "targetperiod" and len(parts) == 3:
            state = _state(conn, user_id)
            payload = (state or {}).get("payload") or {}
            if not state or state["purpose"] != "P4_TARGET_PERIOD":
                raise ValueError("Target form expired")
            period = parts[2].upper()
            if period not in {"DAILY", "WEEKLY", "MONTHLY", "CUSTOM", "CAMPAIGN"}:
                raise ValueError("Choose a valid target period")
            if period == "CAMPAIGN":
                team.require_permission(conn, user_id, "campaigns.manage")
            _set_state(conn, user_id, "P4_TARGET_DATES", payload=payload | {"period_type": period})
            _edit(
                update,
                actions,
                "Enter start and end dates as YYYY-MM-DD, YYYY-MM-DD. The end date is inclusive in this form.",
                InlineKeyboardMarkup([[button("Cancel", "p4:targets")]]),
            )
        elif op == "targetscope" and len(parts) == 3:
            state = _state(conn, user_id)
            payload = (state or {}).get("payload") or {}
            if not state or state["purpose"] != "P4_TARGET_SCOPE":
                raise ValueError("Target form expired")
            scope_type = parts[2]
            if payload.get("period_type") == "CAMPAIGN":
                team.require_permission(conn, user_id, "campaigns.manage")
                if scope_type != "campaign":
                    raise ValueError("Campaign targets must use campaign scope")
                rows = [
                    [button(c["name"][:32], f"p4:targetcampaign:{c['id']}")]
                    for c in conn.execute(
                        "select id,name from campaigns where status <> 'ARCHIVED' order by created_at desc limit 30"
                    ).fetchall()
                ]
            elif scope_type == "team":
                rows = [
                    [button(t["name"][:32], f"p4:targetteam:{t['id']}")]
                    for t in team.list_teams(conn, user_id)
                    if t["active"]
                ]
            elif scope_type == "user":
                rows = [
                    [
                        button(
                            str(m["display_name"] or m["telegram_user_id"])[:32],
                            f"p4:targetuser:{m['telegram_user_id']}",
                        )
                    ]
                    for m in team.list_members(conn, user_id, 100)
                    if m["role"] == "SALESPERSON" and m["account_status"] == "ACTIVE"
                ]
            else:
                raise ValueError("Choose a supported target scope")
            _set_state(
                conn, user_id, "P4_TARGET_CHOOSE", payload=payload | {"scope_type": scope_type}
            )
            _edit(
                update,
                actions,
                "Choose the target scope.",
                InlineKeyboardMarkup(rows + [[button("Cancel", "p4:targets")]]),
            )
        elif op in {"targetteam", "targetuser", "targetcampaign"} and len(parts) == 3:
            state = _state(conn, user_id)
            payload = (state or {}).get("payload") or {}
            if not state or state["purpose"] != "P4_TARGET_CHOOSE":
                raise ValueError("Target form expired")
            if state["payload"].get("period_type") == "CAMPAIGN" and op != "targetcampaign":
                raise ValueError("Campaign targets must use campaign scope")
            if op == "targetcampaign":
                team.require_permission(conn, user_id, "campaigns.manage")
            if op == "targetteam":
                payload["team_id"] = parts[2]
            elif op == "targetuser":
                payload["telegram_user_id"] = int(parts[2])
            else:
                payload["campaign_id"] = parts[2]
            _set_state(conn, user_id, "P4_TARGET_CONFIRM", payload=payload)
            _edit(
                update,
                actions,
                "Confirm target creation. Activity will be counted from existing CRM records in the selected period.",
                InlineKeyboardMarkup(
                    [[button("Confirm Target", "p4:targetyes")], [button("Cancel", "p4:targets")]]
                ),
            )
        elif op == "targetyes":
            state = _state(conn, user_id)
            payload = (state or {}).get("payload") or {}
            if not state or state["purpose"] != "P4_TARGET_CONFIRM":
                raise ValueError("Target confirmation expired")
            team.create_target(
                conn,
                user_id,
                target_type=payload["target_type"],
                target_amount=payload["target_amount"],
                period_type=payload["period_type"],
                starts_at=datetime.fromisoformat(payload["starts_at"]),
                ends_at=datetime.fromisoformat(payload["ends_at"]),
                telegram_user_id=payload.get("telegram_user_id"),
                team_id=UUID(payload["team_id"]) if payload.get("team_id") else None,
                campaign_id=UUID(payload["campaign_id"]) if payload.get("campaign_id") else None,
                currency="ETB" if payload["target_type"] == "VERIFIED_SALES_VALUE" else None,
            )
            _clear_state(conn, user_id)
            _targets(conn, update, actions, user_id)
        elif op == "myfinance":
            team.require_permission(conn, user_id, "commissions.self")
            records = conn.execute(
                """select cr.commission_status,cr.calculated_amount,cr.approved_amount,cr.paid_amount,
                          cr.currency,s.sale_date,s.recorded_sale_price
                   from commission_records cr join sale_records s on s.id=cr.sale_record_id
                   where coalesce(cr.responsible_telegram_user_id,s.responsible_telegram_user_id)=%s
                     and cr.commission_status not in ('CANCELLED','DISPUTED')
                   order by cr.created_at desc limit 20""",
                (user_id,),
            ).fetchall()
            text = "<b>My Commission Records</b>"
            for row in records:
                amount = (
                    row["approved_amount"]
                    if row["approved_amount"] is not None
                    else row["calculated_amount"]
                )
                text += f"\n{row['sale_date']} · {row['currency'].strip()} {amount:,.2f} · {row['commission_status'].replace('_', ' ').title()} · paid {row['paid_amount']:,.2f}"
            if not records:
                text += "\nNo commission records are assigned to you yet."
            _edit(
                update, actions, text, InlineKeyboardMarkup([[button("Team Workspace", "p4:menu")]])
            )
        else:
            _edit(
                update,
                actions,
                "That team action is no longer available.",
                InlineKeyboardMarkup([[button("Team Workspace", "p4:menu")]]),
            )
    except (PermissionError, ValueError, TypeError) as exc:
        logger.info("Phase 4 action rejected (%s)", type(exc).__name__)
        _edit(
            update,
            actions,
            html.escape(str(exc)),
            InlineKeyboardMarkup([[button("Team Workspace", "p4:menu")]]),
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
    purpose = state.get("purpose")
    message = update.effective_message
    if purpose == "P4_TEAM_NEW":
        name = (message.text or "").strip() if message else ""
        team.create_team(conn, user_id, name)
        _clear_state(conn, user_id)
        _edit(
            update, actions, "Team created.", InlineKeyboardMarkup([[button("Teams", "p4:teams")]])
        )
        return True
    if purpose == "P4_TEAM_RENAME":
        name = (message.text or "").strip() if message else ""
        team_id = UUID(state["payload"]["team_id"])
        team.rename_team(conn, user_id, team_id, name)
        _clear_state(conn, user_id)
        _edit(
            update,
            actions,
            "Team renamed.",
            InlineKeyboardMarkup([[button("Open Team", f"p4:team:{team_id}")]]),
        )
        return True
    if purpose == "P4_SETTING_CAPACITY":
        raw = (message.text or "").strip() if message else ""
        try:
            capacity = int(raw)
        except ValueError as exc:
            raise ValueError("Enter a whole number from 1 to 100000") from exc
        team.update_team_settings(conn, user_id, {"max_active_leads_per_salesperson": capacity})
        _clear_state(conn, user_id)
        _team_settings_page(conn, update, actions, user_id)
        return True
    if purpose == "P4_DIST_FILTERS":
        raw = (message.text or "").strip() if message else ""
        if not raw or len(raw) > 1000:
            raise ValueError("Enter one or more comma-separated filters (up to 1000 characters)")
        filters: dict[str, Any] = {}
        for item in raw.split(","):
            key, separator, value = item.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or not value or key in filters:
                raise ValueError("Each filter must use a unique key=value pair")
            if key in {"min_budget", "max_budget"}:
                try:
                    amount = Decimal(value.replace(",", ""))
                except InvalidOperation as exc:
                    raise ValueError(f"{key} must be a valid amount") from exc
                if not amount.is_finite() or amount < 0:
                    raise ValueError(f"{key} must be a nonnegative amount")
                filters[key] = amount
            else:
                filters[key] = value
        if (
            filters.get("min_budget") is not None
            and filters.get("max_budget") is not None
            and filters["min_budget"] > filters["max_budget"]
        ):
            raise ValueError("Minimum budget cannot exceed maximum budget")
        team_id = UUID(state["payload"]["team_id"])
        job = team.preview_distribution(
            conn,
            user_id,
            team_id,
            method=state["payload"]["method"],
            filters=filters,
            idempotency_key=f"telegram:{user_id}:{update_id}",
        )
        _clear_state(conn, user_id)
        _distribution_preview(conn, update, actions, job["id"])
        return True
    if purpose == "P4_HANDOVER_CONTACT":
        query = (message.text or "").strip() if message else ""
        matches = crm.search_contacts(conn, query, 8)
        rows = [
            [
                button(
                    f"{r['name'][:28]} · {r['phone_number'][-12:]}", f"p4:handovercontact:{r['id']}"
                )
            ]
            for r in matches
        ]
        rows.append([button("Cancel", "p4:menu")])
        _edit(
            update,
            actions,
            "Choose the assigned lead for your handover request."
            if matches
            else "No assigned lead in your scope matched that search.",
            InlineKeyboardMarkup(rows),
        )
        return True
    if purpose == "P4_PERFORMANCE_CUSTOM":
        raw = (message.text or "").strip() if message else ""
        pieces = [piece.strip() for piece in raw.replace("/", "-").split(",") if piece.strip()]
        if len(pieces) != 2:
            pieces = raw.split()
        if len(pieces) != 2:
            raise ValueError("Enter two YYYY-MM-DD dates separated by a comma")
        try:
            start_day, last_day = (date.fromisoformat(piece) for piece in pieces)
        except ValueError as exc:
            raise ValueError("Use YYYY-MM-DD for both dates") from exc
        if last_day < start_day or (last_day - start_day).days > 366:
            raise ValueError("Date range must be ordered and no longer than 367 days")
        team_id = (
            UUID(state["payload"]["team_id"]) if state.get("payload", {}).get("team_id") else None
        )
        _set_state(
            conn,
            user_id,
            "P4_PERFORMANCE_CUSTOM",
            payload={
                "team_id": str(team_id) if team_id else None,
                "member_id": state.get("payload", {}).get("member_id"),
                "start_date": start_day.isoformat(),
                "end_date": (last_day + timedelta(days=1)).isoformat(),
            },
        )
        _performance(conn, update, actions, user_id, team_id=team_id, period="CUSTOM")
        _clear_state(conn, user_id)
        return True
    if purpose == "P4_CAMPAIGN_RULES":
        raw = (message.text or "").strip() if message else ""
        fields = [part.strip() for part in raw.split("|")]
        if len(fields) != 6:
            raise ValueError("Enter all six calling rule fields separated by |")
        try:
            start_day = None if fields[0] == "-" else date.fromisoformat(fields[0])
            end_day = None if fields[1] == "-" else date.fromisoformat(fields[1])
            priority, attempts, retry_hours = (int(value) for value in fields[2:5])
            target = None if fields[5] == "-" else Decimal(fields[5].replace(",", ""))
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(
                "Use YYYY-MM-DD dates, whole-number rule values, and a valid target amount"
            ) from exc
        timezone_row = conn.execute(
            "select timezone from user_settings where telegram_user_id=%s", (user_id,)
        ).fetchone()
        zone = ZoneInfo(timezone_row["timezone"] if timezone_row else "Africa/Addis_Ababa")
        starts_at = datetime.combine(start_day, time.min, tzinfo=zone) if start_day else None
        ends_at = (
            datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=zone)
            if end_day
            else None
        )
        campaign_id = UUID(state["payload"]["campaign_id"])
        team.update_campaign_calling_rules(
            conn,
            user_id,
            campaign_id,
            starts_at=starts_at,
            ends_at=ends_at,
            calling_priority=priority,
            max_call_attempts=attempts,
            retry_after_hours=retry_hours,
            performance_target=target,
        )
        _clear_state(conn, user_id)
        _campaign_card(conn, update, actions, user_id, campaign_id)
        return True
    if purpose == "P4_ASSIGN_CONTACT":
        query = (message.text or "").strip() if message else ""
        matches = crm.search_contacts(conn, query, 8)
        rows = [
            [button(f"{r['name'][:28]} · {r['phone_number'][-12:]}", f"p4:assigncontact:{r['id']}")]
            for r in matches
        ]
        rows.append([button("Cancel", "p4:menu")])
        if not matches:
            _edit(
                update,
                actions,
                "No lead in your authorized scope matched that search.",
                InlineKeyboardMarkup(rows),
            )
        else:
            _edit(update, actions, "Choose the lead to assign.", InlineKeyboardMarkup(rows))
        return True
    if purpose == "P4_TARGET_AMOUNT":
        raw = (message.text or "").strip() if message else ""
        try:
            amount = Decimal(raw.replace(",", ""))
        except InvalidOperation as exc:
            raise ValueError("Enter a valid nonnegative target amount") from exc
        if not amount.is_finite() or amount < 0 or amount > Decimal("99999999999999.99"):
            raise ValueError("Target amount is outside the supported range")
        payload = state.get("payload") or {}
        _set_state(
            conn, user_id, "P4_TARGET_PERIOD", payload=payload | {"target_amount": str(amount)}
        )
        rows = [
            [button(period.title(), f"p4:targetperiod:{period}")]
            for period in ("DAILY", "WEEKLY", "MONTHLY", "CUSTOM", "CAMPAIGN")
        ]
        rows.append([button("Cancel", "p4:targets")])
        _edit(update, actions, "Choose the target measurement period.", InlineKeyboardMarkup(rows))
        return True
    if purpose == "P4_TARGET_DATES":
        raw = (message.text or "").strip() if message else ""
        date_parts = [part.strip() for part in raw.replace("/", "-").split(",") if part.strip()]
        if len(date_parts) != 2:
            date_parts = raw.split()
        if len(date_parts) != 2:
            raise ValueError("Enter the start and end dates separated by a comma")
        try:
            start_day, end_day = (date.fromisoformat(part) for part in date_parts)
        except ValueError as exc:
            raise ValueError("Use YYYY-MM-DD for both dates") from exc
        if end_day < start_day:
            raise ValueError("End date must be on or after the start date")
        timezone = crm.ensure_user(conn, user_id, settings)["timezone"]
        zone = ZoneInfo(timezone)
        start = datetime.combine(start_day, time.min, tzinfo=zone)
        end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=zone)
        payload = state.get("payload") or {}
        payload |= {"starts_at": start.isoformat(), "ends_at": end.isoformat()}
        _set_state(conn, user_id, "P4_TARGET_SCOPE", payload=payload)
        if payload["period_type"] == "CAMPAIGN":
            rows = [[button("Campaign", "p4:targetscope:campaign")]]
        else:
            rows = [
                [
                    button("Team", "p4:targetscope:team"),
                    button("Salesperson", "p4:targetscope:user"),
                ]
            ]
        rows.append([button("Cancel", "p4:targets")])
        _edit(
            update,
            actions,
            f"Choose who the {payload['target_type'].replace('_', ' ').lower()} target applies to.",
            InlineKeyboardMarkup(rows),
        )
        return True
    return False
