#!/usr/bin/env python3
"""Real HTTP fixtures for the loopback gateway; no external network or bot login."""
import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

spec = importlib.util.spec_from_file_location("gateway", Path(__file__).parents[1] / "ops/telegram_api_gateway.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)
BOT = "12345:" + "A" * 35
OTHER = "67890:" + "B" * 35


class Upstream(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        if self.headers.get("Transfer-Encoding") == "chunked":
            body = b""
            while True:
                size = int(self.rfile.readline().strip(), 16)
                if not size:
                    self.rfile.readline()
                    break
                body += self.rfile.read(size)
                assert self.rfile.read(2) == b"\r\n"
        else:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.calls.append((self.path, body, self.headers.get("Content-Type")))
        if "getFile" in self.path:
            result = {"ok": True, "result": {"file_id": "fixture", "file_path": "photos/image.jpg"}}
            data = json.dumps(result).encode()
            status = 200
        elif "fail" in self.path:
            data = b'{"ok":false,"error_code":503,"description":"uncertain"}'
            status = 503
        else:
            data, status = body, 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        for token in [BOT, OTHER]:
            (self.root / token / "photos").mkdir(parents=True)
            (self.root / token / "photos/image.jpg").write_bytes(b"fixture image bytes")
            (self.root / token / "binlog").write_bytes(b"private state")
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.proxy = gateway.Gateway(0, self.upstream.server_port, self.root)
        Upstream.calls = []
        for server in [self.upstream, self.proxy]:
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in [self.proxy, self.upstream]:
            server.shutdown()
            server.server_close()
        self.directory.cleanup()

    def request(self, path, method="GET", body=None, headers=None, chunked=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {}, encode_chunked=chunked)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_health_uses_only_local_root(self):
        status, data, _ = self.request("/__health")
        self.assertEqual((status, json.loads(data)), (200, {"ok": True}))
        self.assertEqual(Upstream.calls, [])

    def test_json_and_uncertain_send_are_not_retried(self):
        payload = b'{"chat_id":"fixture","text":"hello"}'
        status, data, _ = self.request(f"/bot{BOT}/sendMessage?x=1", "POST", payload,
                                      {"Content-Type": "application/json"})
        self.assertEqual((status, data), (200, payload))
        self.assertEqual(Upstream.calls, [(f"/bot{BOT}/sendMessage?x=1", payload, "application/json")])
        status, data, _ = self.request(f"/bot{BOT}/fail", "POST", b"{}")
        self.assertEqual(status, 503)
        self.assertFalse(json.loads(data)["ok"])
        self.assertEqual(len(Upstream.calls), 2)

    def test_multipart_and_chunked_streams(self):
        body = b"--fixture\r\nContent-Disposition: form-data; name=photo\r\n\r\n" + b"x" * 150000 + b"\r\n--fixture--\r\n"
        content_type = "multipart/form-data; boundary=fixture"
        for chunked in [False, True]:
            with self.subTest(chunked=chunked):
                source = iter([body[:70000], body[70000:]]) if chunked else body
                status, data, _ = self.request(f"/bot{BOT}/sendPhoto", "POST", source,
                                               {"Content-Type": content_type}, chunked)
                self.assertEqual((status, data), (200, body))
                self.assertEqual(Upstream.calls[-1][1:], (body, content_type))

    def test_file_requires_getfile_grant_and_preserves_bytes(self):
        path = f"/file/bot{BOT}/photos/image.jpg"
        self.assertEqual(self.request(path)[0], 404)
        status, data, _ = self.request(f"/bot{BOT}/getFile", "POST", b"{}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["result"]["file_path"], "photos/image.jpg")
        status, data, headers = self.request(path)
        self.assertEqual((status, data), (200, b"fixture image bytes"))
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        self.assertEqual(self.request(f"/file/bot{OTHER}/photos/image.jpg")[0], 404)
        self.assertEqual(self.request(f"/file/bot{BOT}/binlog")[0], 404)
        # Existing links remain valid when the gateway restarts.
        reopened = gateway.Grants(self.root)
        with reopened.open_file(BOT, "photos/image.jpg") as file:
            self.assertEqual(file.read(), b"fixture image bytes")

    def test_file_traversal_expiry_and_symlink_are_blocked(self):
        self.proxy.grants.remember(BOT, "photos/image.jpg")
        for suffix in ["../binlog", "%2e%2e/binlog", "/etc/passwd", "photos/../../binlog", "photos%5cimage.jpg"]:
            with self.subTest(suffix=suffix):
                self.assertIn(self.request(f"/file/bot{BOT}/{suffix}")[0], [400, 404])
        with self.proxy.grants.connect() as db:
            db.execute("UPDATE grants SET expires=0")
        self.assertEqual(self.request(f"/file/bot{BOT}/photos/image.jpg")[0], 404)
        outside = self.root / "outside"
        outside.write_bytes(b"not a bot file")
        (self.root / BOT / "photos/escape").symlink_to(outside)
        self.assertEqual(self.request(f"/file/bot{BOT}/photos/escape")[0], 400)

    def test_arbitrary_targets_and_absolute_file_paths_are_blocked(self):
        for target in ["/", "/file/botinvalid/a", "http://example.com/", f"/bot{BOT}/../getMe"]:
            with self.subTest(target=target):
                self.assertIn(self.request(target)[0], [400, 404])
        for relative in ["/etc/passwd", "../binlog", "photos//image.jpg"]:
            with self.assertRaises(ValueError):
                self.proxy.grants.remember(BOT, relative)
        self.assertEqual(Upstream.calls, [])

    def test_ambiguous_body_framing_is_rejected(self):
        status, _, _ = self.request(f"/bot{BOT}/sendMessage", "POST", b"{}",
                                    {"Content-Length": "2", "Transfer-Encoding": "chunked"})
        self.assertEqual(status, 400)
        self.assertEqual(Upstream.calls, [])


if __name__ == "__main__":
    unittest.main()
