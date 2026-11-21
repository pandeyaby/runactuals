"""MCP HTTP recording and stdio wrapper, against in-process fakes only."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from runactuals import conventions as c
from runactuals.mcp import FrameDecoder, encode_message, parse_jsonrpc, record_mcp_http, wrap_stdio
from runactuals.proxy import serve_proxy
from runactuals.record import SpanWriter
from runactuals.redact import REDACTED
from runactuals.store import Store

FAKE_TOKEN = "sk-test-mcp-cccccccc"

CHILD = r"""
import json, sys

def read():
    header = b""
    while not header.endswith(b"\r\n\r\n"):
        b = sys.stdin.buffer.read(1)
        if not b:
            return None
        header += b
    length = 0
    for line in header.decode().split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1])
    payload = sys.stdin.buffer.read(length)
    return json.loads(payload.decode())

def write(obj):
    payload = json.dumps(obj).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
    sys.stdout.buffer.flush()

msg = read()
if not msg:
    sys.exit(1)
if msg.get("method") == "tools/call":
    write({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": "ok"}]}})
else:
    write({"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32601, "message": "unknown"}})
"""


class McpTest(unittest.TestCase):
    def test_parse_jsonrpc_json_ndjson_and_sse(self):
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "sum"}}
        self.assertEqual(parse_jsonrpc(json.dumps(req).encode())[0]["method"], "tools/call")
        nd = json.dumps(req) + "\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(len(parse_jsonrpc(nd)), 2)
        sse = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        self.assertEqual(parse_jsonrpc(sse)[0]["result"], {"ok": True})

    def test_frame_decoder_lsp_and_ndjson(self):
        dec = FrameDecoder()
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        frames = encode_message(msg)
        mid = len(frames) // 2
        self.assertEqual(dec.feed(frames[:mid]), [])
        self.assertEqual(dec.feed(frames[mid:])[0]["method"], "initialize")
        dec2 = FrameDecoder()
        self.assertEqual(dec2.feed(b'{"jsonrpc":"2.0","method":"ping"}\n')[0]["method"], "ping")

    def test_http_tools_call_redacts_and_records(self):
        req = {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "lookup", "arguments": {"api_key": FAKE_TOKEN, "q": "hi"}},
        }
        resp = {"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": "ok"}]}}
        spans = record_mcp_http(
            "POST", "/mcp", {"Authorization": f"Bearer {FAKE_TOKEN}"},
            json.dumps(req).encode(), 200, {}, json.dumps(resp).encode(),
            10, 20, "lesson-builder",
        )
        blob = json.dumps(spans)
        self.assertNotIn(FAKE_TOKEN, blob)
        tool = next(s for s in spans if s["attributes"].get(c.GEN_AI_OPERATION) == c.OP_EXECUTE_TOOL)
        self.assertEqual(tool["attributes"][c.GEN_AI_TOOL_NAME], "lookup")
        self.assertIn(REDACTED, tool["attributes"][c.GEN_AI_TOOL_CALL_ARGS])
        self.assertIn("hi", tool["attributes"][c.GEN_AI_TOOL_CALL_ARGS])
        self.assertEqual(tool["parent_span_id"], next(
            s["span_id"] for s in spans if s["attributes"].get(c.GEN_AI_OPERATION) == c.OP_INVOKE_AGENT))

    def test_wrap_stdio_records_tool_call(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(os.path.join(tmp.name, "ra.db"))
        writer = SpanWriter(store=store)
        stdin = io.BytesIO(encode_message({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hi", "token": FAKE_TOKEN}},
        }))
        stdout, stderr = io.BytesIO(), io.BytesIO()
        code = wrap_stdio([sys.executable, "-c", CHILD], writer, agent="wrap-test",
                          stdin=stdin, stdout=stdout, stderr=stderr)
        self.assertEqual(code, 0)
        out = stdout.getvalue()
        self.assertIn(b'"result"', out)
        spans = store.spans()
        blob = json.dumps(spans)
        self.assertNotIn(FAKE_TOKEN, blob)
        tool = next(s for s in spans if s["attributes"].get(c.GEN_AI_TOOL_NAME) == "echo")
        self.assertEqual(tool["attributes"][c.GEN_AI_OPERATION], c.OP_EXECUTE_TOOL)
        self.assertIn("ok", tool["attributes"]["runactuals.proxy.response_preview"])
        store.close()

    def test_proxy_mcp_prefix_forwards_and_records(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        class Fake(ThreadingHTTPServer):
            daemon_threads = True

            def __init__(self):
                super().__init__(("127.0.0.1", 0), self._H)

            class _H(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    n = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(n)
                    msg = json.loads(body)
                    resp = json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                       "result": {"content": [{"type": "text", "text": "pong"}]}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)

        fake = Fake()
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (fake.shutdown(), fake.server_close()))
        store = Store(os.path.join(tmp.name, "ra.db"))
        proxy = serve_proxy(
            SpanWriter(store=store), "127.0.0.1", 0,
            upstreams={"mcp": f"http://127.0.0.1:{fake.server_address[1]}"}, timeout=5,
        )
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (proxy.shutdown(), proxy.server_close(), store.close()))
        url = f"http://127.0.0.1:{proxy.server_address[1]}"
        body = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "ping", "arguments": {}}}).encode()
        req = urllib.request.Request(url + "/mcp", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(json.loads(resp.read())["result"]["content"][0]["text"], "pong")
        tool = next(s for s in store.spans() if s["attributes"].get(c.GEN_AI_TOOL_NAME) == "ping")
        self.assertEqual(tool["attributes"][c.GEN_AI_OPERATION], c.OP_EXECUTE_TOOL)


if __name__ == "__main__":
    unittest.main()
