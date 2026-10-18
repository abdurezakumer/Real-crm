import asyncio
import json
from http.server import BaseHTTPRequestHandler

from realestate_crm.runtime import MAX_WEBHOOK_BYTES, process_webhook


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            self._respond(400, {"ok": False, "error": "invalid content length"})
            return
        if length <= 0 or length > MAX_WEBHOOK_BYTES:
            self._respond(
                413 if length > MAX_WEBHOOK_BYTES else 400,
                {"ok": False, "error": "invalid request size"},
            )
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("update must be a JSON object")
            status, body = asyncio.run(
                process_webhook(payload, self.headers.get("X-Telegram-Bot-Api-Secret-Token"))
            )
            self._respond(status, body)
        except json.JSONDecodeError:
            self._respond(400, {"ok": False, "error": "invalid JSON"})

    def do_GET(self):
        self._respond(405, {"ok": False, "error": "method not allowed"})

    def _respond(self, status: int, body: dict):
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        # Avoid writing Telegram update content or authorization details to logs.
        return
