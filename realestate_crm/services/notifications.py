"""Bounded, idempotent Phase 3 reminder generation and delivery."""

from __future__ import annotations

import html
import json
import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from telegram.error import TelegramError

from realestate_crm.config import Settings
from realestate_crm.database import connection
from realestate_crm.timeutils import local_label

logger = logging.getLogger(__name__)

RETRY_MINUTES = (1, 2, 5, 15, 60)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def _insert(
    conn,
    *,
    key: str,
    kind: str,
    entity_type: str,
    entity_id,
    user_id: int,
    context: dict[str, Any],
    now: datetime,
) -> bool:
    row = conn.execute(
        """insert into notification_deliveries
             (notification_key,notification_type,related_entity_type,related_entity_id,
              message_context,telegram_user_id,chat_id,scheduled_delivery_at,next_attempt_at)
           values (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
           on conflict (notification_key) do nothing returning id""",
        (
            key,
            kind,
            entity_type,
            entity_id,
            json.dumps(context, ensure_ascii=False, default=str),
            user_id,
            user_id,
            now,
            now,
        ),
    ).fetchone()
    return bool(row)


def prepare_notifications(settings: Settings, batch_size: int = 25) -> int:
    now = datetime.now(UTC)
    with connection() as conn:
        limit = max(1, min(batch_size, 100))
        added = 0

        rows = conn.execute(
            """select f.id,f.scheduled_at,f.follow_up_type,coalesce(f.reason,f.notes) reason,
                      c.name,u.telegram_user_id,s.timezone
               from follow_ups f join contacts c on c.id=f.contact_id
               join crm_users u on u.telegram_user_id=c.assigned_telegram_user_id and u.account_status='ACTIVE'
               left join user_settings s on s.telegram_user_id=u.telegram_user_id
               where f.status='PENDING' and f.scheduled_at>=now()
                 and coalesce(s.notification_minutes_before,30)>0
                 and f.scheduled_at<=now()+make_interval(mins=>coalesce(s.notification_minutes_before,30))
                 and not exists (select 1 from notification_deliveries d where d.related_entity_id=f.id
                   and d.telegram_user_id=u.telegram_user_id and d.notification_type='FOLLOW_UP_SOON'
                   and d.message_context->>'scheduled_at'=
                     to_char(f.scheduled_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS.US'))
               order by f.scheduled_at,f.id limit %s""",
            (limit,),
        ).fetchall()
        for row in rows:
            stamp = _stamp(row["scheduled_at"])
            user_id = row["telegram_user_id"]
            added += _insert(
                conn,
                key=f"followup-soon:{row['id']}:{stamp}:{user_id}",
                kind="FOLLOW_UP_SOON",
                entity_type="FOLLOW_UP",
                entity_id=row["id"],
                user_id=user_id,
                context={
                    "name": row["name"][:100],
                    "action": row["follow_up_type"],
                    "reason": (row.get("reason") or "")[:250],
                    "scheduled_at": stamp,
                },
                now=now,
            )

        rows = conn.execute(
            """select f.id,f.scheduled_at,f.follow_up_type,coalesce(f.reason,f.notes) reason,
                      c.name,u.telegram_user_id,s.timezone
               from follow_ups f join contacts c on c.id=f.contact_id
               join crm_users u on u.telegram_user_id=c.assigned_telegram_user_id and u.account_status='ACTIVE'
               left join user_settings s on s.telegram_user_id=u.telegram_user_id
               where f.status='PENDING' and f.scheduled_at<%s
                 and not exists (select 1 from notification_deliveries d
                   where d.related_entity_id=f.id and d.telegram_user_id=u.telegram_user_id
                     and d.notification_type='FOLLOW_UP_OVERDUE'
                     and d.message_context->>'scheduled_at'=
                       to_char(f.scheduled_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS.US'))
               order by f.scheduled_at,f.id limit %s""",
            (now, limit),
        ).fetchall()
        for row in rows:
            user_id = row["telegram_user_id"]
            added += _insert(
                conn,
                key=f"followup-overdue:{row['id']}:{_stamp(row['scheduled_at'])}:{user_id}",
                kind="FOLLOW_UP_OVERDUE",
                entity_type="FOLLOW_UP",
                entity_id=row["id"],
                user_id=user_id,
                context={
                    "name": row["name"][:100],
                    "action": row["follow_up_type"],
                    "reason": (row.get("reason") or "")[:250],
                    "scheduled_at": _stamp(row["scheduled_at"]),
                },
                now=now,
            )

        rows = conn.execute(
            """select v.id,v.scheduled_at,c.name,p.title property_title,u.telegram_user_id
               from property_visits v join contacts c on c.id=v.contact_id
               join sales_opportunities o on o.id=v.opportunity_id
               join crm_users u on u.telegram_user_id=coalesce(o.assigned_telegram_user_id,c.assigned_telegram_user_id)
                 and u.account_status='ACTIVE'
               left join user_settings s on s.telegram_user_id=u.telegram_user_id
               join properties p on p.id=v.property_id
               where v.visit_status in ('SCHEDULED','BUYER_ARRIVED') and v.scheduled_at>=now()
                 and coalesce(s.notification_minutes_before,30)>0
                 and v.scheduled_at<=now()+make_interval(mins=>coalesce(s.notification_minutes_before,30))
                 and not exists (select 1 from notification_deliveries d
                   where d.related_entity_id=v.id and d.telegram_user_id=u.telegram_user_id
                     and d.notification_type='VISIT_SOON'
                     and d.message_context->>'scheduled_at'=
                       to_char(v.scheduled_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS.US'))
               order by v.scheduled_at,v.id limit %s""",
            (limit,),
        ).fetchall()
        for row in rows:
            stamp = _stamp(row["scheduled_at"])
            user_id = row["telegram_user_id"]
            added += _insert(
                conn,
                key=f"visit-soon:{row['id']}:{stamp}:{user_id}",
                kind="VISIT_SOON",
                entity_type="PROPERTY_VISIT",
                entity_id=row["id"],
                user_id=user_id,
                context={
                    "name": row["name"][:100],
                    "property": row["property_title"][:160],
                    "scheduled_at": stamp,
                },
                now=now,
            )

        rows = conn.execute(
            """select v.id,v.scheduled_at,c.name,p.title property_title,u.telegram_user_id
               from property_visits v join contacts c on c.id=v.contact_id
               join properties p on p.id=v.property_id
               join sales_opportunities o on o.id=v.opportunity_id
               join crm_users u on u.telegram_user_id=coalesce(o.assigned_telegram_user_id,c.assigned_telegram_user_id)
                 and u.account_status='ACTIVE'
               where v.visit_status in ('SCHEDULED','BUYER_ARRIVED')
                 and v.scheduled_at<=%s and v.scheduled_at>%s-interval '2 hours'
                 and not exists (select 1 from notification_deliveries d
                   where d.related_entity_id=v.id and d.telegram_user_id=u.telegram_user_id
                     and d.notification_type='VISIT_DUE'
                     and d.message_context->>'scheduled_at'=
                       to_char(v.scheduled_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS.US'))
               order by v.scheduled_at,v.id limit %s""",
            (now, now, limit),
        ).fetchall()
        for row in rows:
            user_id = row["telegram_user_id"]
            added += _insert(
                conn,
                key=f"visit-due:{row['id']}:{_stamp(row['scheduled_at'])}:{user_id}",
                kind="VISIT_DUE",
                entity_type="PROPERTY_VISIT",
                entity_id=row["id"],
                user_id=user_id,
                context={
                    "name": row["name"][:100],
                    "property": row["property_title"][:160],
                    "scheduled_at": _stamp(row["scheduled_at"]),
                },
                now=now,
            )

        closing = conn.execute(
            """select o.id,o.expected_closing_date,c.name,u.telegram_user_id from sales_opportunities o
               join contacts c on c.id=o.contact_id
               join crm_users u on u.telegram_user_id=coalesce(o.assigned_telegram_user_id,c.assigned_telegram_user_id)
                 and u.account_status='ACTIVE'
               left join user_settings s on s.telegram_user_id=u.telegram_user_id
               where o.final_outcome is null
                 and o.expected_closing_date between
                   (now() at time zone coalesce(s.timezone,'Africa/Addis_Ababa'))::date
                   and (now() at time zone coalesce(s.timezone,'Africa/Addis_Ababa'))::date+3
                 and not exists (select 1 from notification_deliveries d
                   where d.related_entity_id=o.id and d.telegram_user_id=u.telegram_user_id
                     and d.notification_type='CLOSING_SOON'
                     and d.message_context->>'expected_closing_date'=o.expected_closing_date::text)
               order by o.expected_closing_date,o.id limit %s""",
            (limit,),
        ).fetchall()
        for row in closing:
            close_date = row["expected_closing_date"].isoformat()
            user_id = row["telegram_user_id"]
            added += _insert(
                conn,
                key=f"closing-soon:{row['id']}:{close_date}:{user_id}",
                kind="CLOSING_SOON",
                entity_type="OPPORTUNITY",
                entity_id=row["id"],
                user_id=user_id,
                context={"name": row["name"][:100], "expected_closing_date": close_date},
                now=now,
            )

        negotiations = conn.execute(
            """select n.id,n.next_action_due,n.next_action,c.name,o.id opportunity_id,u.telegram_user_id
               from negotiation_records n join sales_opportunities o on o.id=n.opportunity_id
               join contacts c on c.id=o.contact_id
               join crm_users u on u.telegram_user_id=coalesce(o.assigned_telegram_user_id,c.assigned_telegram_user_id)
                 and u.account_status='ACTIVE'
               where o.final_outcome is null and n.negotiation_status not in ('REJECTED','EXPIRED','CANCELLED')
                 and n.next_action_due between now() and now()+interval '24 hours'
                 and not exists (select 1 from notification_deliveries d
                   where d.related_entity_id=n.id and d.telegram_user_id=u.telegram_user_id
                     and d.notification_type='NEGOTIATION_DUE'
                     and d.message_context->>'due_at'=
                       to_char(n.next_action_due at time zone 'UTC','YYYY-MM-DD HH24:MI:SS.US'))
               order by n.next_action_due,n.id limit %s""",
            (limit,),
        ).fetchall()
        for row in negotiations:
            stamp = _stamp(row["next_action_due"])
            user_id = row["telegram_user_id"]
            added += _insert(
                conn,
                key=f"negotiation-due:{row['id']}:{stamp}:{user_id}",
                kind="NEGOTIATION_DUE",
                entity_type="NEGOTIATION",
                entity_id=row["id"],
                user_id=user_id,
                context={
                    "name": row["name"][:100],
                    "action": (row.get("next_action") or "Review negotiation")[:250],
                    "due_at": stamp,
                },
                now=now,
            )

        reservations = conn.execute(
            """select r.id,r.expires_at,c.name,p.title property_title,u.telegram_user_id from reservations r
               join sales_opportunities o on o.id=r.opportunity_id join contacts c on c.id=o.contact_id
               join crm_users u on u.telegram_user_id=coalesce(o.assigned_telegram_user_id,c.assigned_telegram_user_id)
                 and u.account_status='ACTIVE'
               join properties p on p.id=r.property_id
               where r.reservation_status in ('REQUESTED','AWAITING_CONFIRMATION','CONFIRMED')
                 and r.expires_at between now() and now()+interval '3 days'
                 and not exists (select 1 from notification_deliveries d
                   where d.related_entity_id=r.id and d.telegram_user_id=u.telegram_user_id
                     and d.notification_type='RESERVATION_EXPIRY'
                     and d.message_context->>'expires_at'=
                       to_char(r.expires_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS.US'))
               order by r.expires_at,r.id limit %s""",
            (limit,),
        ).fetchall()
        for row in reservations:
            stamp = _stamp(row["expires_at"])
            user_id = row["telegram_user_id"]
            added += _insert(
                conn,
                key=f"reservation-expiry:{row['id']}:{stamp}:{user_id}",
                kind="RESERVATION_EXPIRY",
                entity_type="RESERVATION",
                entity_id=row["id"],
                user_id=user_id,
                context={
                    "name": row["name"][:100],
                    "property": row["property_title"][:160],
                    "expires_at": stamp,
                },
                now=now,
            )

        users = conn.execute(
            """select u.telegram_user_id,coalesce(s.timezone,'Africa/Addis_Ababa') timezone
               from crm_users u left join user_settings s on s.telegram_user_id=u.telegram_user_id
               where u.account_status='ACTIVE' and u.role in
                 ('SUPER_ADMIN','SALES_MANAGER','SALESPERSON','OBSERVER')
                 and coalesce(s.daily_overdue_summary_enabled,true)
               order by u.telegram_user_id limit 500"""
        ).fetchall()
        for user in users:
            user_id = user["telegram_user_id"]
            local_now = now.astimezone(ZoneInfo(user["timezone"]))
            if local_now.hour < 9:
                continue
            today = local_now.date().isoformat()
            exists = conn.execute(
                "select 1 from notification_deliveries where notification_key=%s",
                (f"daily-overdue:{user_id}:{today}",),
            ).fetchone()
            if exists:
                continue
            overdue = conn.execute(
                """select
                     (select count(*) from follow_ups f join contacts c on c.id=f.contact_id
                       where f.status='PENDING' and f.scheduled_at<%s and crm_can_access_contact(c.id,%s)) followups,
                     (select count(*) from property_visits v join contacts c on c.id=v.contact_id
                       where v.visit_status in ('SCHEDULED','BUYER_ARRIVED') and v.scheduled_at<%s
                         and crm_can_access_contact(c.id,%s)) visits,
                     (select count(*) from negotiation_records n join sales_opportunities o on o.id=n.opportunity_id
                       join contacts c on c.id=o.contact_id where n.next_action_due<%s
                         and n.negotiation_status not in ('REJECTED','EXPIRED','CANCELLED')
                         and crm_can_access_contact(c.id,%s)) negotiations,
                     (select count(*) from sales_opportunities o join contacts c on c.id=o.contact_id
                       where o.final_outcome is null and o.expected_closing_date between %s::date and %s::date+3
                         and crm_can_access_contact(c.id,%s)) closings""",
                (
                    now,
                    user_id,
                    now,
                    user_id,
                    now,
                    user_id,
                    local_now.date(),
                    local_now.date(),
                    user_id,
                ),
            ).fetchone()
            added += _insert(
                conn,
                key=f"daily-overdue:{user_id}:{today}",
                kind="DAILY_OVERDUE_SUMMARY",
                entity_type="USER",
                entity_id=None,
                user_id=user_id,
                context=dict(overdue),
                now=now,
            )
        return added


def _claim(limit: int = 20) -> list[dict[str, Any]]:
    with connection() as conn:
        return conn.execute(
            """with picked as (
                 select d.id,d.telegram_user_id from notification_deliveries d
                 where d.attempts<5 and d.next_attempt_at<=now()
                   and (d.status='QUEUED' or (d.status='SENDING' and d.updated_at<now()-interval '3 minutes'))
                   and exists(select 1 from crm_users u where u.telegram_user_id=d.telegram_user_id
                     and u.account_status='ACTIVE')
                   and (
                     (d.related_entity_type='USER')
                     or (d.related_entity_type='FOLLOW_UP' and exists(select 1 from follow_ups f
                       join contacts c on c.id=f.contact_id where f.id=d.related_entity_id
                         and c.assigned_telegram_user_id=d.telegram_user_id))
                     or (d.related_entity_type='PROPERTY_VISIT' and exists(select 1 from property_visits v
                       join sales_opportunities o on o.id=v.opportunity_id join contacts c on c.id=o.contact_id
                       where v.id=d.related_entity_id
                         and coalesce(o.assigned_telegram_user_id,c.assigned_telegram_user_id)=d.telegram_user_id))
                     or (d.related_entity_type='OPPORTUNITY' and exists(select 1 from sales_opportunities o
                       join contacts c on c.id=o.contact_id where o.id=d.related_entity_id
                         and coalesce(o.assigned_telegram_user_id,c.assigned_telegram_user_id)=d.telegram_user_id))
                     or (d.related_entity_type='NEGOTIATION' and exists(select 1 from negotiation_records n
                       join sales_opportunities o on o.id=n.opportunity_id join contacts c on c.id=o.contact_id
                       where n.id=d.related_entity_id
                         and coalesce(o.assigned_telegram_user_id,c.assigned_telegram_user_id)=d.telegram_user_id))
                     or (d.related_entity_type='RESERVATION' and exists(select 1 from reservations r
                       join sales_opportunities o on o.id=r.opportunity_id join contacts c on c.id=o.contact_id
                       where r.id=d.related_entity_id
                         and coalesce(o.assigned_telegram_user_id,c.assigned_telegram_user_id)=d.telegram_user_id))
                   )
                   and case d.notification_type
                     when 'FOLLOW_UP_SOON' then exists (
                       select 1 from follow_ups f where f.id=d.related_entity_id and f.status='PENDING'
                         and f.scheduled_at>=now() and d.message_context->>'scheduled_at'=
                           to_char(f.scheduled_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS.US'))
                     when 'FOLLOW_UP_OVERDUE' then exists (
                       select 1 from follow_ups f where f.id=d.related_entity_id and f.status='PENDING'
                         and f.scheduled_at<now() and d.message_context->>'scheduled_at'=
                           to_char(f.scheduled_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS.US'))
                     when 'VISIT_SOON' then exists (
                       select 1 from property_visits v where v.id=d.related_entity_id
                         and v.visit_status in ('SCHEDULED','BUYER_ARRIVED') and v.scheduled_at>=now()
                         and exists (select 1 from sales_opportunities o where o.id=v.opportunity_id
                                     and o.final_outcome is null)
                         and d.message_context->>'scheduled_at'=
                           to_char(v.scheduled_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS.US'))
                     when 'VISIT_DUE' then exists (
                       select 1 from property_visits v where v.id=d.related_entity_id
                         and v.visit_status in ('SCHEDULED','BUYER_ARRIVED') and v.scheduled_at<=now()
                         and v.scheduled_at>now()-interval '2 hours'
                         and exists (select 1 from sales_opportunities o where o.id=v.opportunity_id
                                     and o.final_outcome is null)
                         and d.message_context->>'scheduled_at'=
                           to_char(v.scheduled_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS.US'))
                     when 'CLOSING_SOON' then exists (
                       select 1 from sales_opportunities o where o.id=d.related_entity_id
                         and o.final_outcome is null
                         and o.expected_closing_date between
                           coalesce((select (now() at time zone us.timezone)::date from user_settings us
                                     where us.telegram_user_id=d.telegram_user_id),
                                    (now() at time zone 'Africa/Addis_Ababa')::date)
                           and coalesce((select (now() at time zone us.timezone)::date from user_settings us
                                         where us.telegram_user_id=d.telegram_user_id),
                                        (now() at time zone 'Africa/Addis_Ababa')::date)+3
                         and d.message_context->>'expected_closing_date'=o.expected_closing_date::text)
                     when 'NEGOTIATION_DUE' then exists (
                       select 1 from negotiation_records n join sales_opportunities o on o.id=n.opportunity_id
                         where n.id=d.related_entity_id and o.final_outcome is null
                           and n.negotiation_status not in ('REJECTED','EXPIRED','CANCELLED')
                           and n.next_action_due<=now()+interval '24 hours'
                           and d.message_context->>'action'=left(coalesce(n.next_action,'Review negotiation'),250)
                           and d.message_context->>'due_at'=
                             to_char(n.next_action_due at time zone 'UTC','YYYY-MM-DD HH24:MI:SS.US'))
                     when 'RESERVATION_EXPIRY' then exists (
                       select 1 from reservations r where r.id=d.related_entity_id
                         and r.reservation_status in ('REQUESTED','AWAITING_CONFIRMATION','CONFIRMED')
                         and r.expires_at between now() and now()+interval '3 days'
                         and d.message_context->>'expires_at'=
                           to_char(r.expires_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS.US'))
                     when 'DAILY_OVERDUE_SUMMARY' then split_part(d.notification_key,':',3)=
                       coalesce((select (now() at time zone us.timezone)::date::text from user_settings us
                                 where us.telegram_user_id=d.telegram_user_id),
                                (now() at time zone 'Africa/Addis_Ababa')::date::text)
                     else true
                   end
                 order by d.scheduled_delivery_at,d.id for update skip locked limit %s
               )
               update notification_deliveries d set status='SENDING',attempts=d.attempts+1,updated_at=now()
               from picked left join user_settings u on u.telegram_user_id=picked.telegram_user_id
               where d.id=picked.id returning d.*,coalesce(u.timezone,'Africa/Addis_Ababa') timezone,
                 case when d.related_entity_type='FOLLOW_UP' then exists (
                   select 1 from follow_ups f join contacts c on c.id=f.contact_id
                   where f.id=d.related_entity_id and (c.contact_eligibility='DO_NOT_CONTACT'
                     or c.lead_status='DO_NOT_CONTACT')) else false end contact_restricted""",
            (max(1, min(limit, 50)),),
        ).fetchall()


def _message(row: dict[str, Any], timezone: str) -> str:
    context = row["message_context"] or {}
    name = html.escape(str(context.get("name") or "Buyer"))
    if row["notification_type"] == "FOLLOW_UP_SOON":
        action = html.escape(str(context.get("action") or "Follow-up").replace("_", " ").title())
        stamp = datetime.strptime(context["scheduled_at"], "%Y-%m-%d %H:%M:%S.%f").replace(
            tzinfo=UTC
        )
        warning = (
            "\n<b>DO NOT CONTACT</b>: do not call or message this contact; review the record."
            if row.get("contact_restricted")
            else ""
        )
        return f"<b>Follow-up due soon</b>\n{name} ? {action}\n{html.escape(local_label(stamp, timezone))}{warning}"
    if row["notification_type"] == "FOLLOW_UP_OVERDUE":
        action = html.escape(str(context.get("action") or "Follow-up").replace("_", " ").title())
        warning = (
            "\n<b>DO NOT CONTACT</b>: do not call or message this contact; review the record."
            if row.get("contact_restricted")
            else "\nReview current contact restrictions before calling or messaging."
        )
        return f"<b>Overdue sales action</b>\n{name} ? {action}{warning}"
    if row["notification_type"] in {"VISIT_SOON", "VISIT_DUE"}:
        property_title = html.escape(str(context.get("property") or "Property"))
        if row["notification_type"] == "VISIT_DUE":
            return f"<b>Property visit due</b>\n{name} ? {property_title}\nReview the recorded appointment and log the outcome."
        stamp = datetime.strptime(context["scheduled_at"], "%Y-%m-%d %H:%M:%S.%f").replace(
            tzinfo=UTC
        )
        return f"<b>Upcoming property visit</b>\n{name} ? {property_title}\n{html.escape(local_label(stamp, timezone))}"
    if row["notification_type"] == "CLOSING_SOON":
        return f"<b>Expected closing date approaching</b>\n{name} ? {html.escape(str(context.get('expected_closing_date') or 'Date not recorded'))}\nThis is the agent-recorded expectation; verify the latest status."
    if row["notification_type"] == "NEGOTIATION_DUE":
        due = datetime.strptime(context["due_at"], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
        return f"<b>Negotiation action due</b>\n{name} ? {html.escape(str(context.get('action') or 'Review negotiation'))}\n{html.escape(local_label(due, timezone))}"
    if row["notification_type"] == "RESERVATION_EXPIRY":
        return f"<b>Recorded reservation expiry approaching</b>\n{name} ? {html.escape(str(context.get('property') or 'Property'))}\nCheck the status with an authorized source."
    return (
        "<b>Daily overdue summary</b>\n"
        f"Follow-ups: {context.get('followups', 0)} ? Visits: {context.get('visits', 0)}\n"
        f"Negotiation actions: {context.get('negotiations', 0)} ? Expected closings: {context.get('closings', 0)}"
    )


async def send_phase3_notifications(bot, settings: Settings, limit: int = 20) -> int:
    try:
        prepare_notifications(settings)
        rows = _claim(limit)
    except Exception as exc:
        logger.error("Phase 3 notification preparation failed (%s)", type(exc).__name__)
        return 0
    sent = 0
    for row in rows:
        try:
            await bot.send_message(
                row["chat_id"],
                _message(row, row.get("timezone") or "Africa/Addis_Ababa"),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            attempts = int(row["attempts"])
            retry = attempts < len(RETRY_MINUTES)
            delay = RETRY_MINUTES[min(attempts - 1, len(RETRY_MINUTES) - 1)]
            with connection() as conn:
                conn.execute(
                    """update notification_deliveries set status=%s,
                         next_attempt_at=now()+make_interval(mins=>%s),last_error=%s,updated_at=now()
                       where id=%s""",
                    ("QUEUED" if retry else "FAILED", delay, type(exc).__name__[:100], row["id"]),
                )
            if isinstance(exc, TelegramError):
                logger.info(
                    "Phase 3 notification %s delivery failed (%s)", row["id"], type(exc).__name__
                )
            else:
                logger.error(
                    "Phase 3 notification %s delivery failed (%s)", row["id"], type(exc).__name__
                )
        else:
            with connection() as conn:
                conn.execute(
                    """update notification_deliveries set status='SENT',delivered_at=now(),
                         last_error=null,updated_at=now() where id=%s""",
                    (row["id"],),
                )
            sent += 1
    return sent
