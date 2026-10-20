import csv
import hashlib
import hmac
import html
import json
import logging
import re
import tempfile
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from realestate_crm.config import Settings, get_settings
from realestate_crm.database import connection
from realestate_crm.handlers import dispatch_update
from realestate_crm.services import crm
from realestate_crm.services import outbox as telegram_outbox
from realestate_crm.services import team as team_service

logger = logging.getLogger(__name__)
MAX_WEBHOOK_BYTES = 1024 * 1024


def _sender_id(payload: dict[str, Any]) -> int | None:
    message = (
        payload.get("message") or payload.get("edited_message") or payload.get("channel_post") or {}
    )
    sender = payload.get("callback_query", {}).get("from") or message.get("from")
    value = sender.get("id") if isinstance(sender, dict) else None
    return value if isinstance(value, int) and value > 0 else None


def _authorized_private_chat(payload: dict[str, Any], user_id: int) -> bool:
    callback_message = payload.get("callback_query", {}).get("message") or {}
    message = (
        callback_message
        or payload.get("message")
        or payload.get("edited_message")
        or payload.get("channel_post")
        or {}
    )
    chat = message.get("chat") or {}
    return chat.get("type") == "private" and chat.get("id") == user_id


def _invitation_token(payload: dict[str, Any]) -> str | None:
    message = payload.get("message") or payload.get("edited_message") or {}
    text = message.get("text") if isinstance(message, dict) else None
    if not isinstance(text, str):
        return None
    matched = re.fullmatch(r"/start(?:@[A-Za-z0-9_]+)?\s+([A-Za-z0-9_-]{20,64})", text.strip())
    return matched.group(1) if matched else None


def _build_export(action: dict[str, Any], settings: Settings) -> tuple[Path, int]:
    extension = action["format"]
    if extension not in {"csv", "xlsx"}:
        raise ValueError("Unsupported export format")
    date_from = date_to = None
    export_timezone = action.get("timezone", settings.app_timezone)
    zone = ZoneInfo(export_timezone)
    if action.get("date_from"):
        date_from = datetime.combine(
            datetime.fromisoformat(action["date_from"]).date(), time.min, tzinfo=zone
        ).astimezone(UTC)
        date_to = datetime.combine(
            datetime.fromisoformat(action["date_to"]).date(), time.min, tzinfo=zone
        ).astimezone(UTC)
    file_handle = tempfile.NamedTemporaryFile(
        prefix="crm-export-", suffix=f".{extension}", delete=False
    )
    path = Path(file_handle.name)
    file_handle.close()
    headers = [
        "Contact ID",
        "Name",
        "Phone Number",
        "Lead Status",
        "Interest Level",
        "Call Attempts",
        "Last Called",
        "Next Follow-Up",
        "Eligibility",
    ]
    count = 0
    after_id = None
    workbook = None
    sheet = None
    try:
        with connection() as conn:
            if extension == "csv":
                with path.open("w", encoding="utf-8-sig", newline="") as output:
                    writer = csv.writer(output)
                    writer.writerow(headers)
                    while count < settings.export_max_rows:
                        rows = crm.export_contacts(
                            conn,
                            action["filter"],
                            UUID(action["campaign_id"]) if action.get("campaign_id") else None,
                            settings.export_max_rows - count,
                            date_from,
                            date_to,
                            after_id,
                            1000,
                            action.get("telegram_user_id"),
                        )
                        if not rows:
                            break
                        for row in rows:
                            writer.writerow(_export_row(row, export_timezone))
                        count += len(rows)
                        after_id = UUID(rows[-1]["id"])
            else:
                from openpyxl import Workbook

                workbook = Workbook(write_only=True)
                sheet = workbook.create_sheet("Contacts")
                sheet.append(headers)
                while count < settings.export_max_rows:
                    rows = crm.export_contacts(
                        conn,
                        action["filter"],
                        UUID(action["campaign_id"]) if action.get("campaign_id") else None,
                        settings.export_max_rows - count,
                        date_from,
                        date_to,
                        after_id,
                        1000,
                        action.get("telegram_user_id"),
                    )
                    if not rows:
                        break
                    for row in rows:
                        sheet.append(_export_row(row, export_timezone))
                    count += len(rows)
                    after_id = UUID(rows[-1]["id"])
                workbook.save(path)
        return path, count
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _export_row(row: dict[str, Any], timezone: str) -> list[Any]:
    def stamp(value):
        return value.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M") if value else ""

    def safe_text(value: str) -> str:
        if value.lstrip(" \t\r\n")[:1] in {"=", "+", "-", "@"}:
            return "'" + value
        return value

    return [
        safe_text(row["id"]),
        safe_text(row["name"]),
        safe_text(row["phone_number"]),
        safe_text(row["lead_status"]),
        safe_text(row["interest_level"]),
        row["total_call_attempts"],
        stamp(row["last_called_at"]),
        stamp(row["next_follow_up_at"]),
        safe_text(row["contact_eligibility"]),
    ]


async def _finish_preview(bot: Bot, action: dict[str, Any], settings: Settings) -> None:
    from realestate_crm.services.importing import preview_file
    from realestate_crm.services.storage import download_import
    from realestate_crm.telegram_ui import render_preview

    suffix = Path(action["filename"]).suffix.lower()
    with tempfile.TemporaryDirectory(prefix="crm-preview-") as directory:
        path = Path(directory) / f"source{suffix}"
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


async def _stage_text_import(bot: Bot, action: dict[str, Any], settings: Settings) -> None:
    from realestate_crm.phone import normalize_phone
    from realestate_crm.services.storage import upload_import
    from realestate_crm.telegram_ui import render_preview

    records = action["records"]
    job_id = action["job_id"]
    filename = "Pasted Telegram Leads.csv"
    mapping = {"source_row_number": 0, "name": 1, "phone": 2}
    with tempfile.TemporaryDirectory(prefix="crm-text-import-") as directory:
        path = Path(directory) / "pasted-leads.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["NO", "Name", "Phone"])
            writer.writerows(
                [
                    row.get("source_row_number", ""),
                    row.get("name", ""),
                    row.get("phone", ""),
                ]
                for row in records
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        storage_path = await upload_import(settings, job_id, path, upsert=True)

    preview = [
        {
            "name": (
                f"NO {row['source_row_number']} | {row.get('name') or ''}"
                if row.get("source_row_number")
                else str(row.get("name") or "")
            )[:100],
            "phone": (normalize_phone(row.get("phone")) or str(row.get("phone") or ""))[:50],
        }
        for row in records[:20]
    ]
    with connection() as conn:
        conn.execute(
            """update import_jobs set status='PREVIEW',storage_path=%s,file_sha256=%s,
                 header_mapping=%s,preview=%s,updated_at=now()
               where id=%s and status='VALIDATING'""",
            (
                storage_path,
                digest,
                json.dumps(mapping),
                json.dumps(
                    {"headers": ["NO", "Name", "Phone"], "selected": mapping, "rows": preview}
                ),
                UUID(job_id),
            ),
        )
    text, keyboard = render_preview(filename, preview, job_id)
    recognized = f"Detected {len(records)} lead row(s)."
    skipped = int(action.get("skipped_lines") or 0)
    if skipped:
        recognized += f" Skipped {skipped} line(s) without a detected lead."
    await bot.send_message(
        action["chat_id"],
        f"{recognized}\n\n{text}",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def _send_callback_answers(actions: list[dict[str, Any]], settings: Settings) -> None:
    answers = []
    seen_callback_ids = set()
    for action in actions:
        callback_id = action.get("callback_id") if action.get("type") == "answer" else None
        if callback_id is not None and callback_id not in seen_callback_ids:
            seen_callback_ids.add(callback_id)
            answers.append(action)
    if not answers:
        return
    try:
        async with Bot(settings.telegram_bot_token.get_secret_value()) as bot:
            for action in answers:
                try:
                    await bot.answer_callback_query(
                        callback_query_id=action["callback_id"], text=action.get("text", "")
                    )
                except TelegramError as exc:
                    logger.info("Callback acknowledgement failed (%s)", type(exc).__name__)
    except Exception as exc:
        logger.warning("Callback acknowledgement client failed (%s)", type(exc).__name__)


async def _deliver_outbox_action(bot: Bot, action: dict[str, Any], settings: Settings) -> None:
    kind = action["type"]
    markup_data = action.get("markup")
    markup = InlineKeyboardMarkup.de_json(markup_data, bot) if markup_data else None
    if kind == "send":
        await bot.send_message(
            chat_id=action["chat_id"],
            text=action["text"],
            parse_mode=action.get("parse_mode"),
            reply_markup=markup,
        )
    elif kind == "edit":
        try:
            await bot.edit_message_text(
                chat_id=action["chat_id"],
                message_id=action["message_id"],
                text=action["text"],
                parse_mode=action.get("parse_mode"),
                reply_markup=markup,
            )
        except BadRequest as exc:
            reason = str(exc).lower()
            if "message is not modified" in reason:
                return
            if any(
                marker in reason
                for marker in (
                    "message to edit not found",
                    "message can't be edited",
                    "there is no text in the message to edit",
                )
            ):
                await bot.send_message(
                    chat_id=action["chat_id"],
                    text=action["text"],
                    parse_mode=action.get("parse_mode"),
                    reply_markup=markup,
                )
                return
            raise
    elif kind == "finish_preview":
        await _finish_preview(bot, action, settings)
    elif kind == "stage_text_import":
        await _stage_text_import(bot, action, settings)
    elif kind == "dialer":
        import httpx

        token = settings.telegram_bot_token.get_secret_value()
        message_html = (
            f"<p><b>{html.escape(action['name'])}</b></p>"
            f'<p><a href="{html.escape(action["tel_uri"], quote=True)}">Open phone dialer</a></p>'
            "<p>Telegram will open the dialer. Tap the phone’s call button to start manually.</p>"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendRichMessage",
                json={"chat_id": action["chat_id"], "rich_message": {"html": message_html}},
            )
        response.raise_for_status()
        if not response.json().get("ok"):
            raise RuntimeError("Telegram did not accept the dialer link")
    else:
        raise ValueError("Outbox contains an unsupported Telegram action")


async def _drain_telegram_outbox(settings: Settings, limit: int = 2) -> int:
    try:
        with connection() as conn:
            pending = telegram_outbox.claim_actions(conn, limit)
    except Exception as exc:
        logger.error("Telegram outbox claim failed (%s)", type(exc).__name__)
        return 0
    if not pending:
        return 0

    delivered = 0
    try:
        async with Bot(settings.telegram_bot_token.get_secret_value()) as bot:
            for row in pending:
                action = row["payload"]
                if isinstance(action, str):
                    action = json.loads(action)
                try:
                    await _deliver_outbox_action(bot, action, settings)
                except Exception as exc:
                    try:
                        with connection() as conn:
                            telegram_outbox.mark_failed(conn, row, exc)
                            if (
                                action.get("type") == "stage_text_import"
                                and int(row["attempts"]) >= 8
                            ):
                                conn.execute(
                                    """update import_jobs set status='FAILED',
                                         error_summary=%s,completed_at=now(),updated_at=now()
                                       where id=%s and status='VALIDATING'""",
                                    (
                                        json.dumps(
                                            [
                                                {
                                                    "reason": "Pasted leads could not be staged after retries"
                                                }
                                            ]
                                        ),
                                        UUID(action["job_id"]),
                                    ),
                                )
                    except Exception as store_exc:
                        logger.error(
                            "Telegram outbox failure state could not be saved (%s)",
                            type(store_exc).__name__,
                        )
                    if action.get("type") == "stage_text_import" and int(row["attempts"]) >= 8:
                        try:
                            await bot.send_message(
                                action["chat_id"],
                                "I couldn’t stage the pasted leads after several attempts. Please paste them again later.",
                            )
                        except TelegramError:
                            pass
                    continue
                try:
                    with connection() as conn:
                        telegram_outbox.mark_sent(conn, row["id"])
                    delivered += 1
                except Exception as exc:
                    # The remote send may have succeeded; the five-minute lease allows recovery.
                    logger.error(
                        "Telegram outbox completion could not be saved (%s)",
                        type(exc).__name__,
                    )
    except Exception as exc:
        logger.error("Telegram outbox bot client failed (%s)", type(exc).__name__)
        for row in pending:
            try:
                with connection() as conn:
                    telegram_outbox.mark_failed(conn, row, exc)
            except Exception as store_exc:
                logger.error(
                    "Telegram outbox failure state could not be saved (%s)",
                    type(store_exc).__name__,
                )
    return delivered


async def process_webhook(
    payload: dict[str, Any], secret_header: str | None
) -> tuple[int, dict[str, Any]]:
    settings = get_settings()
    expected = settings.telegram_webhook_secret.get_secret_value()
    if not secret_header or not hmac.compare_digest(secret_header, expected):
        return 403, {"ok": False, "error": "unauthorized"}
    sender_id = _sender_id(payload)
    if sender_id is None:
        return 200, {"ok": True}
    if not _authorized_private_chat(payload, sender_id):
        return 200, {"ok": True}
    update_id = payload.get("update_id")
    if not isinstance(update_id, int) or update_id < 0:
        return 400, {"ok": False, "error": "invalid update"}
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    try:
        update = Update.de_json(payload, bot)
        actions: list[dict[str, Any]] = []
        with connection() as conn:
            display_name = update.effective_user.full_name if update.effective_user else ""
            admin_ids = getattr(settings, "authorized_telegram_user_ids", None)
            if admin_ids is None:
                admin_ids = [settings.authorized_telegram_user_id]
            for admin_id in admin_ids:
                team_service.ensure_bootstrap_admin(
                    conn,
                    admin_id,
                    display_name if sender_id == admin_id else "",
                )
            if sender_id in admin_ids:
                crm.ensure_user(conn, sender_id, settings)
                if sender_id == admin_ids[0]:
                    team_service.bootstrap_legacy_ownership(conn, sender_id, limit=500)
            token = _invitation_token(payload)
            if sender_id not in admin_ids and not token:
                if not team_service.user_is_active(conn, sender_id):
                    return 200, {"ok": True}
            elif token is None and not team_service.user_is_active(conn, sender_id):
                return 200, {"ok": True}
            conn.execute("select set_config('crm.telegram_user_id',%s,true)", (str(sender_id),))
            claimed = conn.execute(
                "insert into processed_telegram_updates (update_id,telegram_user_id) values (%s,%s) on conflict do nothing returning update_id",
                (update_id, sender_id),
            ).fetchone()
            if claimed:
                rate = conn.execute(
                    """insert into telegram_rate_limits (telegram_user_id,window_started_at,request_count)
                       values (%s,date_trunc('minute',now()),1)
                       on conflict (telegram_user_id) do update set
                         request_count=case when telegram_rate_limits.window_started_at=date_trunc('minute',now())
                           then telegram_rate_limits.request_count+1 else 1 end,
                         window_started_at=date_trunc('minute',now()),updated_at=now()
                       returning request_count""",
                    (sender_id,),
                ).fetchone()
                if rate and rate["request_count"] <= 60:
                    actions = dispatch_update(conn, update, settings, update_id)
                    telegram_outbox.enqueue_actions(
                        conn, telegram_user_id=sender_id, update_id=update_id, actions=actions
                    )
        await _send_callback_answers(actions, settings)
        await _drain_telegram_outbox(settings, limit=2)
        return 200, {"ok": True}
    except Exception as exc:
        logger.error("Webhook update %s failed (%s)", update_id, type(exc).__name__)
        return 500, {"ok": False, "error": "processing failed"}


async def process_worker(secret_header: str | None) -> tuple[int, dict[str, Any]]:
    settings = get_settings()
    expected = settings.follow_up_cron_secret.get_secret_value()
    provided = secret_header or ""
    if provided.startswith("Bearer "):
        provided = provided[7:]
    if not hmac.compare_digest(provided, expected):
        return 403, {"ok": False, "error": "unauthorized"}
    try:
        from realestate_crm.worker import run_worker

        async with Bot(settings.telegram_bot_token.get_secret_value()) as bot:
            result = await run_worker(bot, settings)
        result["telegram_actions_sent"] = await _drain_telegram_outbox(settings, limit=2)
        with connection() as conn:
            result["telegram_actions_pruned"] = telegram_outbox.prune_terminal_actions(conn)
        return 200, {"ok": True, **result}
    except Exception as exc:
        logger.error("Scheduled worker failed (%s)", type(exc).__name__)
        return 500, {"ok": False, "error": "worker failed"}
