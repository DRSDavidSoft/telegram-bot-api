#!/usr/bin/env python3
"""Loopback Bot API/file adapter; never log token-bearing request paths.

Extends the deployment pattern of the existing Yekta loopback Telegram relay.
The upstream here is the local maintained Bot API server, not the cloud API.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import threading
import time
from urllib.parse import unquote, urlsplit

TOKEN = r"[0-9]{1,20}:[A-Za-z0-9_-]{20,100}"
API_PATH = re.compile(r"/bot(" + TOKEN + r")/([A-Za-z][A-Za-z0-9]{0,63})\Z")
FILE_PATH = re.compile(r"/file/bot(" + TOKEN + r")/(.+)\Z")
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
MAX_BODY = 2 * 1024 * 1024 * 1024
CHUNK = 64 * 1024
API_IO_TIMEOUT = 600  # Preserve the existing publisher's multipart timeout.


class Grants:
    def __init__(self, root, ttl=86400):
        self.root = Path(root).resolve(strict=True)
        self.database = self.root / "gateway-grants.sqlite3"
        self.ttl = ttl
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS grants (token_hash TEXT, path TEXT, expires REAL, PRIMARY KEY(token_hash,path))")
        os.chmod(self.database, 0o600)

    def connect(self):
        return sqlite3.connect(self.database, timeout=10)

    def safe_path(self, token, relative):
        if (not re.fullmatch(TOKEN, token) or not relative or relative.startswith("/")
                or "\\" in relative or any(ord(c) < 32 for c in relative)
                or any(p in {"", ".", ".."} for p in relative.split("/"))):
            raise ValueError("invalid file path")
        directory = (self.root / token).resolve()
        if directory.parent != self.root:
            raise ValueError("invalid bot directory")
        result = (directory / relative).resolve()
        if not result.is_relative_to(directory):
            raise ValueError("file escaped bot directory")
        return result

    def remember(self, token, relative):
        self.safe_path(token, relative)
        with self.connect() as db:
            db.execute("DELETE FROM grants WHERE expires < ?", (time.time(),))
            db.execute("INSERT OR REPLACE INTO grants VALUES (?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), relative, time.time() + self.ttl))

    def open_file(self, token, relative):
        path = self.safe_path(token, relative)
        with self.connect() as db:
            row = db.execute("SELECT expires FROM grants WHERE token_hash=? AND path=?",
                             (hashlib.sha256(token.encode()).hexdigest(), relative)).fetchone()
        if not row or row[0] < time.time():
            raise FileNotFoundError("file grant absent or expired")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        file = os.fdopen(descriptor, "rb")
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            file.close()
            raise FileNotFoundError("not a regular file")
        return file


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, upstream_port, root):
        self.upstream_port = upstream_port
        self.grants = Grants(root)
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(("127.0.0.1", port), Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        # Default traceback/error logging can contain request URLs or filenames.
        return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TelegramLoopbackGateway"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(API_IO_TIMEOUT)
        self.headers_started = False

    def log_message(self, *_args):
        return

    def reply(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.headers_started = True
        self.close_connection = True
        self.wfile.write(body)

    def body_chunks(self, length, chunked):
        remaining = length
        total = 0
        while chunked or remaining:
            if chunked:
                line = self.rfile.readline(128)
                if not line.endswith(b"\r\n"):
                    raise ValueError("invalid chunk")
                remaining = int(line.split(b";", 1)[0].strip(), 16)
                if remaining < 0 or remaining > MAX_BODY - total:
                    raise ValueError("invalid chunk size")
                if remaining == 0:
                    # Trailers are deliberately unsupported; n8n does not need them.
                    if self.rfile.readline(8192) != b"\r\n":
                        raise ValueError("unsupported trailer")
                    return
            while remaining:
                data = self.rfile.read(min(CHUNK, remaining))
                if not data:
                    raise ValueError("incomplete body")
                remaining -= len(data)
                total += len(data)
                if total > MAX_BODY:
                    raise ValueError("request too large")
                yield data
            if chunked and self.rfile.read(2) != b"\r\n":
                raise ValueError("invalid chunk terminator")

    def forward(self, token, method):
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        if transfer not in {"", "chunked"} or (transfer and "Content-Length" in self.headers):
            raise ValueError("ambiguous request framing")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= MAX_BODY:
            self.reply(413, {"ok": False, "description": "Request body too large"})
            return
        upstream = http.client.HTTPConnection("127.0.0.1", self.server.upstream_port, timeout=API_IO_TIMEOUT)
        try:
            upstream.putrequest(self.command, self.path, skip_accept_encoding=True)
            for key, value in self.headers.items():
                if key.lower() not in HOP_HEADERS:
                    upstream.putheader(key, value)
            upstream.putheader("Connection", "close")
            upstream.putheader("Transfer-Encoding" if transfer else "Content-Length",
                               "chunked" if transfer else str(length))
            upstream.endheaders()
            for chunk in self.body_chunks(length, bool(transfer)):
                if transfer:
                    upstream.send(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                else:
                    upstream.send(chunk)
            if transfer:
                upstream.send(b"0\r\n\r\n")
            response = upstream.getresponse()
            body = None
            if method.lower() == "getfile" and response.status == 200:
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise ValueError("unexpected file metadata")
                result = json.loads(body)
                relative = result.get("result", {}).get("file_path") if result.get("ok") else None
                if relative:
                    # Standard Bot API mode must return a relative path. Refuse
                    # absolute local-mode paths rather than exposing host state.
                    self.server.grants.remember(token, relative)
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS:
                    self.send_header(key, value)
            if body is not None:
                self.send_header("Content-Length", str(len(body)))
            elif response.getheader("Content-Length") is not None:
                self.send_header("Content-Length", response.getheader("Content-Length"))
            self.send_header("Connection", "close")
            self.end_headers()
            self.headers_started = True
            self.close_connection = True
            if body is not None:
                self.wfile.write(body)
            else:
                shutil.copyfileobj(response, self.wfile, CHUNK)
        finally:
            upstream.close()

    def dispatch(self):
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise ValueError("invalid request target")
            path = unquote(parsed.path, errors="strict")
            if self.command == "GET" and path == "/__health":
                upstream = http.client.HTTPConnection("127.0.0.1", self.server.upstream_port, timeout=2)
                try:
                    upstream.request("GET", "/")
                    ready = upstream.getresponse().status == 404
                finally:
                    upstream.close()
                self.reply(200 if ready else 503, {"ok": ready})
                return
            match = FILE_PATH.fullmatch(path)
            if self.command == "GET" and match and not parsed.query:
                with self.server.grants.open_file(*match.groups()) as file:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(os.fstat(file.fileno()).st_size))
                    self.send_header("Cache-Control", "private, no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.headers_started = True
                    self.close_connection = True
                    shutil.copyfileobj(file, self.wfile, CHUNK)
                return
            match = API_PATH.fullmatch(path)
            if not match:
                self.reply(404, {"ok": False, "description": "Not Found"})
                return
            # Forward original encoding/query unchanged to the upstream API.
            self.forward(*match.groups())
        except (FileNotFoundError, PermissionError):
            if not self.headers_started:
                self.reply(404, {"ok": False, "description": "File not available"})
        except (ValueError, UnicodeError):
            if not self.headers_started:
                self.reply(400, {"ok": False, "description": "Invalid request"})
        except (OSError, http.client.HTTPException, sqlite3.Error):
            # Never retry a send with an uncertain upstream outcome.
            if not self.headers_started:
                self.reply(502, {"ok": False, "description": "Local Bot API unavailable; outcome may be uncertain"})
        finally:
            self.close_connection = True

    do_GET = dispatch
    do_POST = dispatch


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18444)
    parser.add_argument("--upstream-port", type=int, default=8081)
    parser.add_argument("--state-dir", default="/var/lib/telegram-bot-api")
    args = parser.parse_args()
    os.umask(0o077)
    Gateway(args.port, args.upstream_port, args.state_dir).serve_forever()
