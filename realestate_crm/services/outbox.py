import json
import logging
from typing import Any

from psycopg import Connection
from telegram import InlineKeyboardMarkup

logger = logging.getLogger(__name__)

SUPPORTED_ACTIONS = {"send", "edit", "finish_preview", "dialer", "stage_text_import"}
MAX_ACTIONS_PER_UPDATE = 100


def enqueue_actions(
    conn: Connection,
    *,
    telegram_user_id: int,
    update_id: int,
    actions: list[dict[str, Any]],
) -> int:
    """Persist user-visible actions in the same transaction as their CRM update."""
    if len(actions) > MAX_ACTIONS_PER_UPDATE:
        raise ValueError("Telegram update produced too many outbound actions")
    queued = 0
    for sequence, action in enumerate(actions):
        kind = action.get("type")
        if kind == "answer":
            continue
        if kind not in SUPPORTED_ACTIONS:
            raise ValueError("Telegram action type is not supported by the durable outbox")
        payload = dict(action)
        markup = payload.get("markup")
        if markup is not None:
            if not isinstance(markup, InlineKeyboardMarkup):
                raise TypeError("Telegram reply markup must be an InlineKeyboardMarkup")
            payload["markup"] = markup.to_dict()
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            """insert into telegram_outbox(telegram_user_id,update_id,sequence,action_type,payload)
               values (%s,%s,%s,%s,%s::jsonb) on conflict(update_id,sequence) do nothing""",
            (telegram_user_id, update_id, sequence, kind.upper(), serialized),
        )
        queued += 1
    return queued


def claim_actions(conn: Connection, limit: int = 20) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), 50))
    return conn.execute(
        """with exhausted as (
             update telegram_outbox set status='FAILED',locked_until=null,
               last_error='delivery lease expired at retry limit',updated_at=now()
             where status='SENDING' and locked_until<now() and attempts>=8 returning id
           ), eligible as (
             select id from telegram_outbox
             where (status='QUEUED' and available_at<=now())
                or (status='SENDING' and locked_until<now() and attempts<8)
             order by created_at,id
             for update skip locked limit %s
           )
           update telegram_outbox o set status='SENDING',attempts=o.attempts+1,
             locked_until=now()+interval '5 minutes',updated_at=now()
           from eligible where o.id=eligible.id returning o.id,o.action_type,o.payload,o.attempts""",
        (bounded_limit,),
    ).fetchall()


def mark_sent(conn: Connection, action_id: Any) -> None:
    conn.execute(
        """update telegram_outbox set status='SENT',locked_until=null,last_error=null,
             updated_at=now(),sent_at=now() where id=%s and status='SENDING'""",
        (action_id,),
    )


def mark_failed(conn: Connection, action: dict[str, Any], error: Exception) -> None:
    attempts = int(action["attempts"])
    terminal = attempts >= 8
    backoff_seconds = min(15 * 2 ** max(attempts - 1, 0), 900)
    conn.execute(
        """update telegram_outbox set status=%s,locked_until=null,
             available_at=now()+(%s*interval '1 second'),last_error=%s,updated_at=now()
           where id=%s and status='SENDING'""",
        (
            "FAILED" if terminal else "QUEUED",
            backoff_seconds,
            type(error).__name__[:80],
            action["id"],
        ),
    )
    logger.warning(
        "Telegram outbox action %s failed on attempt %s (%s)",
        action["id"],
        attempts,
        type(error).__name__,
    )


def prune_terminal_actions(conn: Connection, limit: int = 1000) -> int:
    result = conn.execute(
        """with expired as (
             select id from telegram_outbox
             where status in ('SENT','FAILED') and updated_at<now()-interval '30 days'
             order by updated_at,id limit %s
           ) delete from telegram_outbox o using expired e where o.id=e.id""",
        (max(1, min(int(limit), 5000)),),
    )
    return result.rowcount
