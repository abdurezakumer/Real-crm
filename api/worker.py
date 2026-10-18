import asyncio
import json
from http.server import BaseHTTPRequestHandler

from realestate_crm.runtime import process_worker


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        status, body = asyncio.run(process_worker(self.headers.get("Authorization")))
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        self.send_response(405)
        self.end_headers()

    def log_message(self, format, *args):
        return
