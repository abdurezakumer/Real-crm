import asyncio
import hashlib
import html
import json
import logging
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from realestate_crm.config import Settings
from realestate_crm.database import connection
from realestate_crm.services.ai import AIError, generate_ai_result
from realestate_crm.services.backups import (
    build_customer_profiles_backup,
    build_database_backup,
)
from realestate_crm.services.importing import ColumnMappingError, preview_file, process_import_batch
from realestate_crm.services.notifications import send_phase3_notifications
from realestate_crm.services.storage import (
    backup_download_url,
    delete_import,
    download_import,
    upload_backup,
    upload_import,
)
from realestate_crm.telegram_ui import button, render_preview
from realestate_crm.timeutils import local_label

logger = logging.getLogger(__name__)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _column_keyboard(job_id: str, headers: list[str], field: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button(f"{index + 1}. {header[:35] or '(blank)'}", f"icol:{job_id}:{field}:{index}")]
            for index, header in enumerate(headers[:40])
        ]
    )


async def _fail_validation(
    bot, job: dict[str, Any], error_type: str, storage_path: str | None = None
) -> None:
    with connection() as conn:
        conn.execute(
            """update import_jobs set status='FAILED',storage_path=coalesce(%s,storage_path),
                 error_summary=%s,completed_at=now(),updated_at=now() where id=%s""",
            (
                storage_path,
                json.dumps([{"reason": f"File validation failed ({error_type})"}]),
                job["id"],
            ),
        )
    try:
        await bot.send_message(
            job["chat_id"],
            "I couldn’t read this file. Check that it is a valid .xlsx or .csv contact list, then upload it again.",
        )
    except TelegramError:
        pass


async def _prepare_received_import(bot, settings: Settings) -> bool:
    with connection() as conn:
        job = conn.execute(
            """select * from import_jobs where status='RECEIVED'
               or (status='VALIDATING' and updated_at < now()-interval '15 minutes')
               order by created_at limit 1 for update skip locked"""
        ).fetchone()
        if not job:
            return False
        conn.execute(
            "update import_jobs set status='VALIDATING',updated_at=now() where id=%s", (job["id"],)
        )
    job_id = job["id"]
    suffix = Path(job["original_filename"]).suffix.lower()
    if suffix not in {".xlsx", ".csv"}:
        await _fail_validation(bot, job, "unsupported file extension")
        return True
    with tempfile.TemporaryDirectory(prefix="crm-import-") as directory:
        local_path = Path(directory) / f"source{suffix}"
        try:
            telegram_file = await bot.get_file(
                job["telegram_file_id"], read_timeout=30, connect_timeout=10
            )
            await telegram_file.download_to_drive(
                custom_path=local_path, read_timeout=45, write_timeout=45, connect_timeout=10
            )
        except Exception as exc:
            await _fail_validation(bot, job, type(exc).__name__)
            return True
        if local_path.stat().st_size > settings.max_import_file_mb * 1024 * 1024:
            await _fail_validation(bot, job, "file exceeds size limit")
            return True
        digest = _hash_file(local_path)
        try:
            storage_path = await upload_import(settings, str(job_id), local_path)
        except Exception as exc:
            await _fail_validation(bot, job, type(exc).__name__)
            return True
        try:
            preview = preview_file(local_path, job["original_filename"])
            with connection() as conn:
                conn.execute(
                    """update import_jobs set status='PREVIEW',storage_path=%s,file_sha256=%s,
                         header_mapping=%s,preview=%s,updated_at=now() where id=%s""",
                    (
                        storage_path,
                        digest,
                        json.dumps(preview.mapping),
                        json.dumps(
                            {
                                "headers": preview.headers,
                                "selected": preview.mapping,
                                "rows": preview.rows,
                            }
                        ),
                        job_id,
                    ),
                )
            text, keyboard = render_preview(job["original_filename"], preview.rows, str(job_id))
            await bot.send_message(
                job["chat_id"], text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
        except ColumnMappingError as exc:
            selected = {
                key: indices[0]
                for key, indices in exc.candidates.items()
                if len(indices) == 1 and key in {"name", "phone"}
            }
            with connection() as conn:
                conn.execute(
                    """update import_jobs set status='PREVIEW',storage_path=%s,file_sha256=%s,
                         header_mapping=null,preview=%s,updated_at=now() where id=%s""",
                    (
                        storage_path,
                        digest,
                        json.dumps({"headers": exc.headers, "selected": selected}),
                        job_id,
                    ),
                )
            missing = next((key for key in ("name", "phone") if key not in selected), "name")
            await bot.send_message(
                job["chat_id"],
                f"I found {len(exc.headers)} columns but need you to select the {missing} column.",
                reply_markup=_column_keyboard(str(job_id), exc.headers, missing),
            )
        except Exception as exc:
            await _fail_validation(bot, job, type(exc).__name__, storage_path)
            logger.info("Import preview failed for job %s (%s)", job_id, type(exc).__name__)
    return True


async def _finish_preview(bot, settings: Settings, action: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="crm-preview-") as directory:
        path = Path(directory) / f"source{Path(action['filename']).suffix.lower()}"
        await download_import(settings, action["storage_path"], path)
        preview = preview_file(path, action["filename"], action["mapping"])
    with connection() as conn:
        conn.execute(
            "update import_jobs set header_mapping=%s,preview=%s,updated_at=now() where id=%s and status='PREVIEW'",
            (
                json.dumps(preview.mapping),
                json.dumps(
                    {"headers": preview.headers, "selected": preview.mapping, "rows": preview.rows}
                ),
                UUID(action["job_id"]),
            ),
        )
    text, keyboard = render_preview(action["filename"], preview.rows, action["job_id"])
    await bot.edit_message_text(
        chat_id=action["chat_id"],
        message_id=action["message_id"],
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def _claim_import() -> dict[str, Any] | None:
    with connection() as conn:
        job = conn.execute(
            """select * from import_jobs where status='QUEUED'
               or (status='PROCESSING' and updated_at < now()-interval '15 minutes')
               order by created_at limit 1 for update skip locked"""
        ).fetchone()
        if not job:
            return None
        conn.execute(
            "update import_jobs set status='PROCESSING',updated_at=now() where id=%s", (job["id"],)
        )
        job["status"] = "PROCESSING"
    return job


def _import_progress(job_id: UUID) -> dict[str, Any] | None:
    with connection() as conn:
        return conn.execute("select * from import_jobs where id=%s", (job_id,)).fetchone()


async def _process_import(bot, settings: Settings) -> bool:
    job = await _claim_import()
    if not job:
        return False
    if not job["storage_path"] or not job["header_mapping"]:
        with connection() as conn:
            conn.execute(
                "update import_jobs set status='FAILED',error_summary='Missing staged file or column mapping',completed_at=now() where id=%s",
                (job["id"],),
            )
        await bot.send_message(
            job["chat_id"],
            "Import could not start because the staged file or column selection is missing.",
        )
        return True
    with tempfile.TemporaryDirectory(prefix="crm-worker-") as directory:
        path = Path(directory) / f"source{Path(job['original_filename']).suffix.lower()}"
        try:
            await download_import(settings, job["storage_path"], path)
            batch_count = 0
            latest = job
            while batch_count < 10:
                with connection() as conn:
                    locked = conn.execute(
                        "select * from import_jobs where id=%s and status='PROCESSING' for update skip locked",
                        (job["id"],),
                    ).fetchone()
                    if not locked:
                        break
                    result = process_import_batch(
                        conn, locked, path, settings.import_batch_size, settings.max_import_rows
                    )
                latest = _import_progress(job["id"]) or latest
                batch_count += 1
                if result["status"] in {"COMPLETED", "FAILED"}:
                    break
            if latest["status"] in {"COMPLETED", "FAILED"}:
                await _notify_import_complete(bot, latest)
                if latest.get("storage_path"):
                    try:
                        await delete_import(settings, latest["storage_path"])
                        with connection() as conn:
                            conn.execute(
                                "update import_jobs set storage_path=null,updated_at=now() where id=%s",
                                (job["id"],),
                            )
                    except Exception as exc:
                        logger.info("Import object cleanup failed (%s)", type(exc).__name__)
            elif batch_count:
                with connection() as conn:
                    conn.execute(
                        "update import_jobs set status='QUEUED',updated_at=now() where id=%s and status='PROCESSING'",
                        (job["id"],),
                    )
                await bot.send_message(
                    job["chat_id"],
                    f"Import progress: {latest['imported_rows']:,} contacts processed so far. It will continue automatically.",
                )
        except Exception as exc:
            logger.error("Import worker failed for job %s (%s)", job["id"], type(exc).__name__)
            with connection() as conn:
                conn.execute(
                    """update import_jobs set status='QUEUED',updated_at=now(),
                       error_summary=error_summary || %s::jsonb where id=%s and status='PROCESSING'""",
                    (
                        json.dumps([{"reason": f"Temporary worker error: {type(exc).__name__}"}]),
                        job["id"],
                    ),
                )
    return True


async def _notify_import_complete(bot, job: dict[str, Any]) -> None:
    errors = job.get("error_summary") or []
    if isinstance(errors, str):
        try:
            errors = json.loads(errors)
        except json.JSONDecodeError:
            errors = [{"reason": "Import could not be completed"}]
    text = (
        f"📥 Import {job['status'].lower()}\n"
        f"Rows scanned: {job.get('total_rows') or 0:,}\n"
        f"Imported or updated: {job['imported_rows']:,}\n"
        f"Skipped: {job['skipped_rows']:,}  ·  Duplicates: {job['duplicate_rows']:,}\n"
        f"Invalid rows: {job['invalid_rows']:,}"
    )
    if errors:
        text += "\n\nExamples:\n" + "\n".join(
            f"Row {item.get('row', '?')}: {item.get('reason', 'Invalid row')}"
            for item in errors[:8]
        )
    await bot.send_message(job["chat_id"], text, parse_mode=ParseMode.HTML)


def _claim_reminder(conn) -> dict[str, Any] | None:
    row = conn.execute(
        """select d.follow_up_id, d.telegram_user_id, f.scheduled_at, f.follow_up_type,
                  c.id as contact_id, c.name, c.phone_number, coalesce(s.timezone,'Africa/Addis_Ababa') as timezone
           from reminder_deliveries d join follow_ups f on f.id=d.follow_up_id
           join contacts c on c.id=f.contact_id
           left join user_settings s on s.telegram_user_id=d.telegram_user_id
           where f.status='PENDING' and f.scheduled_at<=now() and d.attempts<5
             and c.assigned_telegram_user_id=d.telegram_user_id
             and exists(select 1 from crm_users u where u.telegram_user_id=d.telegram_user_id
               and u.account_status='ACTIVE')
             and (d.status in ('PENDING','FAILED') or (d.status='SENDING' and d.updated_at<now()-interval '15 minutes'))
           order by f.scheduled_at limit 1 for update of d skip locked"""
    ).fetchone()
    if not row:
        return None
    conn.execute(
        "update reminder_deliveries set status='SENDING',attempts=attempts+1,updated_at=now(),last_error=null where follow_up_id=%s",
        (row["follow_up_id"],),
    )
    return row


async def _send_due_reminders(bot, limit: int = 20) -> int:
    sent = 0
    for _ in range(limit):
        with connection() as conn:
            row = _claim_reminder(conn)
        if not row:
            break
        try:
            when = local_label(row["scheduled_at"], row["timezone"])
            text = f"⏰ Follow-up due: {row['follow_up_type']} with {row['name']} ({when})."
            await bot.send_message(
                row["telegram_user_id"],
                text,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Open contact", callback_data=f"lead:{row['contact_id']}"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "Mark complete", callback_data=f"fdone:{row['follow_up_id']}"
                            )
                        ],
                    ]
                ),
            )
            with connection() as conn:
                conn.execute(
                    "update reminder_deliveries set status='SENT',sent_at=now(),updated_at=now() where follow_up_id=%s",
                    (row["follow_up_id"],),
                )
            sent += 1
        except TelegramError as exc:
            with connection() as conn:
                conn.execute(
                    "update reminder_deliveries set status='FAILED',last_error=%s,updated_at=now() where follow_up_id=%s",
                    (type(exc).__name__, row["follow_up_id"]),
                )
            logger.info("Reminder send failed (%s)", type(exc).__name__)
    return sent


async def _cleanup_import_objects(bot, settings: Settings, limit: int = 25) -> int:
    with connection() as conn:
        conn.execute("delete from user_input_state where expires_at <= now()")
        conn.execute(
            """with expired_ai as (
                 select id from ai_jobs where status in ('COMPLETED','FAILED','CANCELLED')
                   and created_at < now()-interval '90 days'
                 order by created_at,id limit 1000 for update skip locked
               ) delete from ai_jobs j using expired_ai e where j.id=e.id"""
        )
        conn.execute(
            """with expired as (
                 select update_id from processed_telegram_updates
                 where processed_at < now()-interval '30 days'
                 order by processed_at limit 5000 for update skip locked
               ) delete from processed_telegram_updates p using expired e
                 where p.update_id=e.update_id"""
        )
        jobs = conn.execute(
            """select id,chat_id,storage_path,status from import_jobs
               where storage_path is not null and status in ('PREVIEW','CANCELLED','COMPLETED','FAILED')
                 and updated_at < now() - make_interval(hours => %s)
               order by updated_at limit %s""",
            (settings.import_retention_hours, limit),
        ).fetchall()
    removed = 0
    for job in jobs:
        try:
            await delete_import(settings, job["storage_path"])
            with connection() as conn:
                conn.execute(
                    """update import_jobs set storage_path=null,
                         status=case when status='PREVIEW' then 'CANCELLED' else status end,
                         preview=null,header_mapping=case when status='PREVIEW' then null else header_mapping end,
                         completed_at=case when status='PREVIEW' then now() else completed_at end,
                         updated_at=now() where id=%s""",
                    (job["id"],),
                )
            if job["status"] == "PREVIEW":
                await bot.send_message(
                    job["chat_id"],
                    "The unconfirmed import preview expired. Upload the file again to continue.",
                )
            removed += 1
        except Exception as exc:
            logger.info("Import retention cleanup failed (%s)", type(exc).__name__)
    return removed


def _claim_export() -> dict[str, Any] | None:
    with connection() as conn:
        job = conn.execute(
            """select * from export_jobs where attempts < 3
               and (status='QUEUED' or (status='PROCESSING' and updated_at < now()-interval '15 minutes'))
               order by created_at limit 1 for update skip locked"""
        ).fetchone()
        if not job:
            return None
        conn.execute(
            """update export_jobs set status='PROCESSING',attempts=attempts+1,
                 last_error=null,updated_at=now() where id=%s""",
            (job["id"],),
        )
        job["status"] = "PROCESSING"
        job["attempts"] += 1
    return job


def _record_export_failure(job: dict[str, Any], error_type: str) -> None:
    with connection() as conn:
        conn.execute(
            """update export_jobs set status=case when attempts >= 3 then 'FAILED' else 'QUEUED' end,
                 last_error=%s,completed_at=case when attempts >= 3 then now() else null end,
                 updated_at=now() where id=%s and status='PROCESSING'""",
            (error_type, job["id"]),
        )


async def _process_export(bot, settings: Settings) -> bool:
    job = _claim_export()
    if not job:
        return False
    path = None
    try:
        # Import at runtime to avoid a module cycle; export generation is outside the webhook.
        from realestate_crm.runtime import _build_export

        date_from = job["date_from"].isoformat() if job["date_from"] else None
        date_to = job["date_to_exclusive"].isoformat() if job["date_to_exclusive"] else None
        action = {
            "format": job["export_format"],
            "filter": job["filter_name"],
            "telegram_user_id": job["telegram_user_id"],
            "campaign_id": str(job["campaign_id"]) if job["campaign_id"] else None,
            "date_from": date_from,
            "date_to": date_to,
            "timezone": job["timezone"],
        }
        path, count = await asyncio.to_thread(_build_export, action, settings)
        label = "XLSX" if job["export_format"] == "xlsx" else "CSV"
        caption = f"{label} export · {count:,} contacts"
        if count >= settings.export_max_rows:
            caption += f" (limited to {settings.export_max_rows:,})"
        await bot.send_document(
            job["chat_id"],
            document=path,
            filename=f"crm_export.{job['export_format']}",
            caption=caption,
        )
        with connection() as conn:
            conn.execute(
                """update export_jobs set status='COMPLETED',completed_at=now(),updated_at=now()
                   where id=%s and status='PROCESSING'""",
                (job["id"],),
            )
    except Exception as exc:
        error_type = type(exc).__name__
        _record_export_failure(job, error_type)
        logger.info("Export job %s failed (%s)", job["id"], error_type)
        if job["attempts"] >= 3:
            try:
                await bot.send_message(
                    job["chat_id"],
                    "The export failed after three attempts. Please try again later.",
                )
            except TelegramError:
                pass
    finally:
        if path:
            path.unlink(missing_ok=True)
    return True


def _claim_daily_backup() -> dict[str, Any] | None:
    with connection() as conn:
        return conn.execute(
            """update crm_backup_state set lease_until=now()+interval '10 minutes',
                 retry_after=null,updated_at=now()
               where id=true and (lease_until is null or lease_until<=now())
                 and (customer_backup_pending
                   or (notification_pending and (retry_after is null or retry_after<=now()))
                   or (not notification_pending and
                       (last_success_at is null or last_success_at<=now()-interval '24 hours')))
               returning *"""
        ).fetchone()


def _mark_daily_customer_backup_pending(storage_path: str, summary: dict[str, Any]) -> None:
    with connection() as conn:
        conn.execute(
            """update crm_backup_state set last_storage_path=%s,last_file_size_bytes=%s,
                 last_table_count=%s,last_row_count=%s,customer_backup_pending=true,
                 notification_pending=false,retry_after=null,lease_until=null,
                 last_error=null,updated_at=now() where id=true""",
            (
                storage_path,
                summary["file_size_bytes"],
                summary["table_count"],
                summary["row_count"],
            ),
        )


def _mark_daily_backup_ready(
    storage_path: str,
    summary: dict[str, Any],
    customer_storage_path: str,
    customer_summary: dict[str, Any],
) -> None:
    with connection() as conn:
        conn.execute(
            """update crm_backup_state set last_success_at=now(),last_storage_path=%s,
                 last_file_size_bytes=%s,last_table_count=%s,last_row_count=%s,
                 last_customer_storage_path=%s,last_customer_file_size_bytes=%s,
                 last_customer_count=%s,last_customer_related_row_count=%s,
                 last_customer_relation_count=%s,customer_backup_pending=false,
                 notification_pending=true,retry_after=now(),last_error=null,
                 lease_until=now()+interval '10 minutes',updated_at=now()
               where id=true""",
            (
                storage_path,
                summary["file_size_bytes"],
                summary["table_count"],
                summary["row_count"],
                customer_storage_path,
                customer_summary["file_size_bytes"],
                customer_summary["contact_count"],
                customer_summary["related_row_count"],
                customer_summary["relation_count"],
            ),
        )


def _mark_daily_backup_notified() -> None:
    with connection() as conn:
        conn.execute(
            """update crm_backup_state set last_notified_at=now(),notification_pending=false,
                 retry_after=null,lease_until=null,last_error=null,updated_at=now()
               where id=true"""
        )


def _defer_daily_backup_retry(error_type: str) -> None:
    with connection() as conn:
        conn.execute(
            """update crm_backup_state set last_error=%s,
                 retry_after=now()+interval '1 hour',lease_until=now()+interval '1 hour',
                 updated_at=now() where id=true""",
            (error_type[:100],),
        )


async def _send_daily_backup_links(
    bot, settings: Settings, database_storage_path: str, customer_storage_path: str
) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    database_url = await backup_download_url(settings, database_storage_path)
    customer_url = await backup_download_url(settings, customer_storage_path)
    recipients = getattr(settings, "authorized_telegram_user_ids", None)
    if recipients is None:
        recipients = [settings.authorized_telegram_user_id]
    for recipient_id in recipients:
        await bot.send_message(
            chat_id=recipient_id,
            text=(
                f"Your daily CRM Excel backups are ready - {timestamp}. "
                "These private links expire in 7 days:\n"
                f"Full CRM database: {database_url}\n"
                f"Customer profiles and linked history: {customer_url}"
            ),
            disable_web_page_preview=True,
        )


async def _process_daily_backup(bot, settings: Settings) -> bool:
    try:
        job = _claim_daily_backup()
    except Exception as exc:
        logger.error("Daily Excel backup claim failed (%s)", type(exc).__name__)
        return False
    if not job:
        return False

    try:
        if (
            job["notification_pending"]
            and job["last_storage_path"]
            and job["last_customer_storage_path"]
        ):
            await _send_daily_backup_links(
                bot, settings, job["last_storage_path"], job["last_customer_storage_path"]
            )
            _mark_daily_backup_notified()
            return True

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        with tempfile.TemporaryDirectory(prefix="crm-daily-backup-") as directory:
            if job["customer_backup_pending"] and job["last_storage_path"]:
                customer_filename = f"real_estate_customer_profiles_{timestamp}.xlsx"
                customer_path = Path(directory) / customer_filename
                customer_summary = await asyncio.to_thread(
                    build_customer_profiles_backup, customer_path
                )
                customer_storage_path = await upload_backup(
                    settings, customer_filename, customer_path
                )
                summary = {
                    "file_size_bytes": job["last_file_size_bytes"],
                    "table_count": job["last_table_count"],
                    "row_count": job["last_row_count"],
                }
                _mark_daily_backup_ready(
                    job["last_storage_path"], summary, customer_storage_path, customer_summary
                )
                await _send_daily_backup_links(
                    bot, settings, job["last_storage_path"], customer_storage_path
                )
                _mark_daily_backup_notified()
                logger.info(
                    "Daily customer profile backup completed (%s customers, %s linked rows, %s bytes)",
                    customer_summary["contact_count"],
                    customer_summary["related_row_count"],
                    customer_summary["file_size_bytes"],
                )
                return True

            filename = f"real_estate_crm_backup_{timestamp}.xlsx"
            path = Path(directory) / filename
            summary = await asyncio.to_thread(build_database_backup, path)
            storage_path = await upload_backup(settings, filename, path)
            _mark_daily_customer_backup_pending(storage_path, summary)
            logger.info(
                "Daily full CRM backup saved (%s tables, %s rows, %s bytes); "
                "customer workbook queued for the next worker run",
                summary["table_count"],
                summary["row_count"],
                summary["file_size_bytes"],
            )
            return True
    except Exception as exc:
        error_type = type(exc).__name__
        try:
            _defer_daily_backup_retry(error_type)
        except Exception as state_exc:
            logger.error("Daily Excel backup retry state failed (%s)", type(state_exc).__name__)
        logger.error("Daily Excel backup failed (%s)", error_type)
        return False


def _claim_ai_job() -> dict[str, Any] | None:
    with connection() as conn:
        job = conn.execute(
            """select * from ai_jobs where attempts < 2 and
                 (status='QUEUED' or (status='PROCESSING' and updated_at < now()-interval '3 minutes'))
               order by created_at limit 1 for update skip locked"""
        ).fetchone()
        if not job:
            return None
        conn.execute(
            "update ai_jobs set status='PROCESSING',attempts=attempts+1,updated_at=now() where id=%s",
            (job["id"],),
        )
        job["status"] = "PROCESSING"
        job["attempts"] = job.get("attempts", 0) + 1
    return job


def _reserve_ai_usage(job: dict[str, Any], daily_limit: int) -> bool:
    """Atomically count and reserve one provider request for this user."""
    with connection() as conn:
        conn.execute("select pg_advisory_xact_lock(%s)", (job["telegram_user_id"],))
        count = conn.execute(
            "select count(*) as used from ai_usage where telegram_user_id=%s "
            "and recorded_at>=now()-interval '24 hours'",
            (job["telegram_user_id"],),
        ).fetchone()["used"]
        if count >= daily_limit:
            return False
        inserted = conn.execute(
            """insert into ai_usage (job_id,telegram_user_id,operation,attempt,status)
               values (%s,%s,%s,%s,'RESERVED') on conflict (job_id,attempt) do nothing returning id""",
            (job["id"], job["telegram_user_id"], job["job_type"], job["attempts"]),
        ).fetchone()
        return inserted is not None


def _ai_result_message(
    job_type: str, result: dict[str, Any], job_id: str, opportunity_id: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    esc = html.escape
    if job_type == "CALL_SCRIPT":
        lines = [
            "<b>Call script draft</b>",
            "",
            f"<b>Opening</b>\n{esc(result['opening'])}",
            f"<b>Reason</b>\n{esc(result['reason_for_calling'])}",
            "<b>Questions</b>",
            *[f"• {esc(item)}" for item in result["questions"]],
            f"<b>Next step</b>\n{esc(result['next_step'])}",
        ]
        if result.get("alternatives"):
            lines.extend(
                [
                    "<b>Alternative wording</b>",
                    *[f"• {esc(item)}" for item in result["alternatives"]],
                ]
            )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    button("Regenerate", f"ai:regenerate:{job_id}"),
                    button("Close", f"ai:close:{job_id}"),
                ]
            ]
        )
    elif job_type == "STRUCTURE_NOTES":
        labels = {
            "property_type": "Property type",
            "preferred_location": "Location",
            "min_budget": "Minimum budget",
            "max_budget": "Maximum budget",
            "currency": "Currency",
            "purchase_purpose": "Purpose",
            "bedrooms": "Bedrooms",
            "preferred_payment_plan": "Payment preference",
            "expected_timeline": "Timeline",
            "genuine_interest": "Interest expressed",
            "followup_text": "Follow-up note",
        }
        lines = ["<b>Review structured call notes</b>"]
        for key, label in labels.items():
            value = result.get(key)
            if value is not None:
                lines.append(f"{esc(label)}: {esc(str(value))}")
        if result.get("followup_requested"):
            lines.append("Follow-up requested: date and time need confirmation before scheduling.")
        if result.get("missing_information"):
            lines.append("Missing / unclear: " + esc(", ".join(result["missing_information"])))
        keyboard = InlineKeyboardMarkup(
            [
                [button("Confirm and Save", f"ai:confirm:{job_id}")],
                [
                    button("Edit Notes", f"ai:edit:{job_id}"),
                    button("Discard", f"ai:discard:{job_id}"),
                ],
            ]
        )
    elif job_type == "OBJECTION":
        lines = [
            "<b>Objection help</b>",
            f"Response: {esc(result['respectful_response'])}",
            f"Question: {esc(result['clarifying_question'])}",
            f"Next action: {esc(result['possible_next_action'])}",
            f"Stop when: {esc(result['stop_when'])}",
        ]
        keyboard = InlineKeyboardMarkup([[button("Close", f"ai:close:{job_id}")]])
    elif job_type == "FOLLOWUP_DRAFT":
        lines = ["<b>Follow-up message draft</b>", esc(result["message"])]
        if result.get("do_not_send"):
            lines.append("<b>Do not send this message.</b> This contact declined further contact.")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        button("Save Draft", f"ai:save:{job_id}"),
                        button("Cancel", f"ai:close:{job_id}"),
                    ]
                ]
            )
        else:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        button("Copy Message", f"ai:copy:{job_id}"),
                        button("Edit", f"ai:edit:{job_id}"),
                    ],
                    [button("Regenerate", f"ai:regenerate:{job_id}")],
                    [
                        button("Save as Note", f"ai:save:{job_id}"),
                        button("Mark as Sent", f"ai:sent:{job_id}"),
                    ],
                    [button("Cancel", f"ai:close:{job_id}")],
                ]
            )
    elif job_type == "OPPORTUNITY_ASSIST":
        lines = [
            "<b>Opportunity summary and suggested action</b>",
            esc(result["summary"]),
            f"<b>Suggested action</b>\n{esc(result['suggested_action'])}",
            f"<b>Reason</b>\n{esc(result['reason'])}",
        ]
        if result.get("do_not_contact"):
            lines.append("<b>Respect the contact restriction. Do not call or message.</b>")
        if result.get("followup_draft"):
            lines.extend(["<b>Optional draft for agent review</b>", esc(result["followup_draft"])])
        buttons = [[button("Close", f"ai:close:{job_id}")]]
        if opportunity_id:
            buttons.insert(0, [button("Open Opportunity", f"p3:op:{opportunity_id}")])
        keyboard = InlineKeyboardMarkup(buttons)
    else:
        lines = ["<b>Sales intelligence</b>"]
        for label, key in (
            ("Observations", "observations"),
            ("Recommendations", "recommendations"),
            ("Caveats", "caveats"),
        ):
            values = result.get(key) or []
            if values:
                lines.append(f"<b>{label}</b>")
                lines.extend(f"• {esc(item)}" for item in values)
        keyboard = InlineKeyboardMarkup([[button("Close", f"ai:close:{job_id}")]])
    message = "\n".join(lines)
    message = re.sub(r"</?b>", "", message)
    return html.unescape(message)[:3900], keyboard


def _ai_contact_allowed(conn, contact_id: UUID) -> bool:
    contact = conn.execute(
        """select c.contact_eligibility,c.lead_status,c.conversion_state,
                  q.qualification_status
           from contacts c left join lead_qualifications q on q.contact_id=c.id
           where c.id=%s""",
        (contact_id,),
    ).fetchone()
    return bool(
        contact
        and contact["contact_eligibility"] == "ELIGIBLE"
        and contact["lead_status"]
        not in {"DO_NOT_CONTACT", "NOT_INTERESTED", "DISQUALIFIED", "CONVERTED"}
        and contact["conversion_state"] != "CONVERTED"
        and contact.get("qualification_status") != "DISQUALIFIED"
    )


def _ai_opportunity_allowed(conn, opportunity_id: UUID, contact_id: UUID, user_id: int) -> bool:
    row = conn.execute(
        """select o.final_outcome,o.assigned_telegram_user_id,c.contact_eligibility,c.lead_status,
                  c.conversion_state,q.qualification_status
           from sales_opportunities o join contacts c on c.id=o.contact_id
           left join lead_qualifications q on q.contact_id=c.id
           where o.id=%s and o.contact_id=%s""",
        (opportunity_id, contact_id),
    ).fetchone()
    return bool(
        row
        and row["final_outcome"] is None
        and row["contact_eligibility"] == "ELIGIBLE"
        and row["lead_status"]
        not in {"DO_NOT_CONTACT", "NOT_INTERESTED", "DISQUALIFIED", "CONVERTED"}
        and row["conversion_state"] != "CONVERTED"
        and row.get("qualification_status") != "DISQUALIFIED"
        and row["assigned_telegram_user_id"] in {None, user_id}
    )


async def _process_ai_job(bot, settings: Settings) -> bool:
    job = _claim_ai_job()
    if not job:
        return False
    if not getattr(settings, "ai_enabled", False):
        reason = "AI assistant is disabled"
        with connection() as conn:
            conn.execute(
                "update ai_jobs set status='FAILED',last_error=%s,completed_at=now(),updated_at=now() where id=%s",
                (reason, job["id"]),
            )
        await bot.send_message(job["chat_id"], "AI is disabled. No CRM data was changed.")
        return True
    if job.get("contact_id"):
        with connection() as conn:
            contact_allowed = _ai_contact_allowed(conn, job["contact_id"])
        if not contact_allowed:
            with connection() as conn:
                conn.execute(
                    """update ai_jobs set status='CANCELLED',last_error='contact_ineligible',
                         input_payload='{}'::jsonb,result_payload='{}'::jsonb,
                         completed_at=now(),updated_at=now() where id=%s""",
                    (job["id"],),
                )
            await bot.send_message(
                job["chat_id"],
                "The contact is no longer eligible for AI assistance. The queued request was cancelled.",
            )
            return True
    if job.get("opportunity_id"):
        with connection() as conn:
            opportunity_allowed = (
                _ai_opportunity_allowed(
                    conn, job["opportunity_id"], job["contact_id"], job["telegram_user_id"]
                )
                if job.get("contact_id")
                else False
            )
        if not opportunity_allowed:
            with connection() as conn:
                conn.execute(
                    """update ai_jobs set status='CANCELLED',last_error='opportunity_ineligible',
                         input_payload='{}'::jsonb,result_payload='{}'::jsonb,
                         completed_at=now(),updated_at=now() where id=%s""",
                    (job["id"],),
                )
            await bot.send_message(
                job["chat_id"],
                "This opportunity is no longer active or available to you. The queued AI request was cancelled.",
            )
            return True
    if not _reserve_ai_usage(job, settings.ai_daily_request_limit):
        with connection() as conn:
            conn.execute(
                "update ai_jobs set status='FAILED',last_error='daily_limit',completed_at=now(),updated_at=now() where id=%s",
                (job["id"],),
            )
        await bot.send_message(
            job["chat_id"],
            "The configured AI request limit for the last 24 hours has been reached. Core CRM features remain available.",
        )
        return True
    try:
        result, usage = await generate_ai_result(
            settings, job["job_type"], job["input_payload"] or {}
        )
        with connection() as conn:
            conn.execute(
                """update ai_usage set status='SUCCEEDED',input_tokens=%s,output_tokens=%s
                   where job_id=%s and attempt=%s and status='RESERVED'""",
                (usage.get("input_tokens"), usage.get("output_tokens"), job["id"], job["attempts"]),
            )
            contact_allowed = (
                _ai_contact_allowed(conn, job["contact_id"]) if job.get("contact_id") else True
            )
            opportunity_allowed = (
                _ai_opportunity_allowed(
                    conn, job["opportunity_id"], job["contact_id"], job["telegram_user_id"]
                )
                if job.get("opportunity_id") and job.get("contact_id")
                else True
            )
            if contact_allowed and opportunity_allowed:
                conn.execute(
                    """update ai_jobs set status='COMPLETED',result_payload=%s,last_error=null,
                         completed_at=now(),updated_at=now() where id=%s""",
                    (json.dumps(result, ensure_ascii=False), job["id"]),
                )
            else:
                conn.execute(
                    """update ai_jobs set status='CANCELLED',last_error='contact_ineligible',
                         input_payload='{}'::jsonb,result_payload='{}'::jsonb,
                         completed_at=now(),updated_at=now() where id=%s""",
                    (job["id"],),
                )
        if not contact_allowed or not opportunity_allowed:
            await bot.send_message(
                job["chat_id"],
                "The contact became ineligible while the AI request was running. The result was discarded.",
            )
            return True
        text, keyboard = _ai_result_message(
            job["job_type"],
            result,
            str(job["id"]),
            str(job["opportunity_id"]) if job.get("opportunity_id") else None,
        )
        await bot.send_message(job["chat_id"], text, reply_markup=keyboard)
    except AIError as exc:
        with connection() as conn:
            conn.execute(
                "update ai_usage set status='FAILED' where job_id=%s and attempt=%s and status='RESERVED'",
                (job["id"], job["attempts"]),
            )
            retry = exc.retryable and job["attempts"] < 2
            conn.execute(
                """update ai_jobs set status=%s,last_error=%s,completed_at=case when %s='FAILED' then now() else null end,
                     updated_at=now() where id=%s""",
                (
                    "QUEUED" if retry else "FAILED",
                    type(exc).__name__,
                    "QUEUED" if retry else "FAILED",
                    job["id"],
                ),
            )
        if not retry:
            await bot.send_message(
                job["chat_id"],
                "The AI request could not be completed. Your CRM data was not changed; try again later.",
            )
        logger.info("AI job %s failed (%s)", job["id"], type(exc).__name__)
    except TelegramError:
        # Keep the completed, durable result available for a later interaction.
        logger.info("AI result notification failed for job %s", job["id"])
    return True


async def run_worker(bot, settings: Settings) -> dict[str, Any]:
    prepared = 0
    try:
        if await _prepare_received_import(bot, settings):
            prepared = 1
    except Exception as exc:
        logger.error("Import preview worker failed (%s)", type(exc).__name__)
    processed = 0
    try:
        if await _process_import(bot, settings):
            processed = 1
    except Exception as exc:
        logger.error("Import batch worker failed (%s)", type(exc).__name__)
    reminded = await _send_due_reminders(bot)
    phase3_reminders = await send_phase3_notifications(bot, settings)
    exported = await _process_export(bot, settings)
    ai_processed = 0
    try:
        if await _process_ai_job(bot, settings):
            ai_processed = 1
    except Exception as exc:
        logger.error("AI job worker failed (%s)", type(exc).__name__)
    cleaned = await _cleanup_import_objects(bot, settings)
    daily_backup = await _process_daily_backup(bot, settings)
    return {
        "import_previews": prepared,
        "import_jobs": processed,
        "reminders_sent": reminded,
        "phase3_notifications_sent": phase3_reminders,
        "exports_processed": int(exported),
        "ai_jobs_processed": ai_processed,
        "import_objects_cleaned": cleaned,
        "daily_excel_backup_processed": int(daily_backup),
    }
