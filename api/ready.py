import json
import logging
from http.server import BaseHTTPRequestHandler
from typing import Any

from realestate_crm.config import get_settings
from realestate_crm.database import connection

logger = logging.getLogger(__name__)


def readiness_status() -> tuple[int, dict[str, Any]]:
    try:
        get_settings()
        with connection() as conn:
            result = conn.execute(
                """select bool_and(to_regclass('public.' || required_table) is not null) ready
                   from unnest(array['processed_telegram_updates','telegram_outbox','crm_users',
                     'contacts','follow_ups','campaigns','sales_opportunities',
                     'crm_backup_state']) required_table"""
            ).fetchone()
            if not result or result["ready"] != 1:
                raise RuntimeError("database readiness query returned no result")
    except Exception as exc:
        logger.error("Readiness check failed (%s)", type(exc).__name__)
        return 503, {"ok": False, "ready": False, "service": "real-estate-calling-crm"}
    return 200, {"ok": True, "ready": True, "service": "real-estate-calling-crm"}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = readiness_status()
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        self.send_response(405)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        return
