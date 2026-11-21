"""LLM HTTP proxy: forwards to a local fake provider and records redacted spans."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from runactuals import conventions as c
from runactuals.model import group_runs
from runactuals.prices import cost_usd
from runactuals.proxy import build_spans, infer_route, serve_proxy, sse_payloads, upstream_url
from runactuals.record import SpanWriter
from runactuals.redact import REDACTED
from runactuals.store import Store

FAKE_OPENAI_KEY = "sk-test-aaaaaaaaaaaaaaaa"
FAKE_ANT_KEY = "sk-ant-bbbbbbbbbbbbbbbb"
FAKE_GEMINI_KEY = "AIzaSyFakeTestKeyAAAAAAAAAAAaa"


class FakeAPI(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _FakeHandler)
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self.handler = self._default

    def _default(self, method, path, headers, body):
        if path.startswith("/v1/chat/completions") or path.startswith("/v1/responses"):
            if b'"stream": true' in body or b'"stream":true' in body:
                payload = (
                    b'data: {"id":"c","choices":[{"delta":{"content":"hi"}}]}\n\n'
                    b'data: {"usage":{"prompt_tokens":10,"completion_tokens":20},'
                    b'"choices":[{"delta":{},"finish_reason":"stop"}],"model":"gpt-4o-mini"}\n\n'
                    b"data: [DONE]\n\n"
                )
                return 200, {"Content-Type": "text/event-stream"}, payload
            return 200, {"Content-Type": "application/json"}, json.dumps({
                "id": "chatcmpl-1", "model": "gpt-4o-mini",
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "hello"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            }).encode()
        if path.startswith("/v1/messages"):
            return 200, {"Content-Type": "application/json"}, json.dumps({
                "id": "msg_1", "model": "claude-sonnet-4-20250514", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "hello"}],
                "usage": {"input_tokens": 8, "output_tokens": 16},
            }).encode()
        if "generateContent" in path or "streamGenerateContent" in path:
            return 200, {"Content-Type": "application/json"}, json.dumps({
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "hello"}]}}],
                "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5},
            }).encode()
        if "embedContent" in path or path.startswith("/v1/embeddings"):
            return 200, {"Content-Type": "application/json"}, json.dumps({
                "embedding": {"values": [0.1, 0.2]},
                "usageMetadata": {"promptTokenCount": 4, "totalTokenCount": 4},
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            }).encode()
        return 404, {"Content-Type": "application/json"}, b'{"error":"not mocked"}'

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class _FakeHandler(BaseHTTPRequestHandler):
    server: FakeAPI

    def log_message(self, *args):
        pass

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {k: v for k, v in self.headers.items()}
        self.server.requests.append((method, self.path, headers, body))
        status, hdrs, data = self.server.handler(method, self.path, headers, body)
        self.send_response(status)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


class ProxyHarness:
    def __init__(self, db):
        self.fake = FakeAPI()
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        self.store = Store(db)
        writer = SpanWriter(store=self.store, service_name="test-proxy")
        origin = self.fake.origin
        self.proxy = serve_proxy(
            writer, "127.0.0.1", 0,
            upstreams={"openai": origin, "anthropic": origin, "gemini": origin, "mcp": origin},
            agent="lesson-builder", timeout=5,
        )
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.proxy.server_address[1]}"

    def request(self, path, body=None, headers=None, method="POST"):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def close(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.fake.shutdown()
        self.fake.server_close()
        self.store.close()


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = ProxyHarness(os.path.join(self.tmp.name, "ra.db"))

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def test_healthz_does_not_hit_upstream(self):
        status, _, body = self.h.request("/healthz", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["role"], "proxy")
        self.assertEqual(self.h.fake.requests, [])

    def test_openai_chat_records_tokens_cost_and_forwards_key(self):
        status, _, body = self.h.request("/openai/v1/chat/completions", {
            "model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}],
        }, headers={"Authorization": f"Bearer {FAKE_OPENAI_KEY}"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["usage"]["prompt_tokens"], 10)
        # The upstream saw the real key (needed to make the call).
        _method, path, headers, _ = self.h.fake.requests[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers.get("Authorization"), f"Bearer {FAKE_OPENAI_KEY}")
        spans = self.h.store.spans()
        stored = json.dumps(spans)
        self.assertNotIn(FAKE_OPENAI_KEY, stored)
        self.assertNotIn("sk-test-", stored)
        chat = next(s for s in spans if s["attributes"].get(c.GEN_AI_OPERATION) == c.OP_CHAT)
        a = chat["attributes"]
        self.assertEqual(a[c.GEN_AI_REQUEST_MODEL], "gpt-4o-mini")
        self.assertEqual(a[c.GEN_AI_INPUT_TOKENS], 10)
        self.assertEqual(a[c.GEN_AI_OUTPUT_TOKENS], 20)
        self.assertEqual(a[c.GEN_AI_PROVIDER], "openai")
        self.assertEqual(a[c.RA_COST_USD], cost_usd("gpt-4o-mini", 10, 20))
        self.assertGreater(a["runactuals.proxy.latency_ms"], 0)
        self.assertIn(REDACTED, a["runactuals.proxy.request_headers"])
        runs = group_runs(spans)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].agent, "lesson-builder")

    def test_implicit_v1_path_and_streaming_usage(self):
        status, _, body = self.h.request("/v1/chat/completions", {
            "model": "gpt-4o-mini", "stream": True, "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(status, 200)
        self.assertIn(b"data: [DONE]", body)
        self.assertEqual(len(sse_payloads(body)), 2)
        chat = next(s for s in self.h.store.spans() if s["attributes"].get(c.GEN_AI_OPERATION) == c.OP_CHAT)
        self.assertEqual(chat["attributes"][c.GEN_AI_INPUT_TOKENS], 10)
        self.assertEqual(chat["attributes"][c.GEN_AI_OUTPUT_TOKENS], 20)

    def test_anthropic_and_gemini_and_embeddings(self):
        st, _, _ = self.h.request("/anthropic/v1/messages", {
            "model": "claude-sonnet-4-20250514", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        }, headers={"x-api-key": FAKE_ANT_KEY, "anthropic-version": "2023-06-01"})
        self.assertEqual(st, 200)
        st, _, _ = self.h.request(
            f"/gemini/v1beta/models/gemini-2.0-flash:generateContent?key={FAKE_GEMINI_KEY}",
            {"contents": [{"parts": [{"text": "hi"}]}]},
        )
        self.assertEqual(st, 200)
        st, _, _ = self.h.request(
            "/gemini/v1beta/models/text-embedding-004:embedContent?key=" + FAKE_GEMINI_KEY,
            {"content": {"parts": [{"text": "hi"}]}},
        )
        self.assertEqual(st, 200)
        stored = json.dumps(self.h.store.spans())
        self.assertNotIn(FAKE_ANT_KEY, stored)
        self.assertNotIn(FAKE_GEMINI_KEY, stored)
        self.assertNotIn("AIza", stored)
        by_model = {s["attributes"].get(c.GEN_AI_REQUEST_MODEL): s
                    for s in self.h.store.spans() if c.GEN_AI_REQUEST_MODEL in s["attributes"]}
        ant = by_model["claude-sonnet-4-20250514"]
        self.assertEqual(ant["attributes"][c.GEN_AI_PROVIDER], "anthropic")
        self.assertEqual(ant["attributes"][c.GEN_AI_INPUT_TOKENS], 8)
        self.assertEqual(ant["attributes"][c.RA_COST_USD], cost_usd("claude-sonnet-4-20250514", 8, 16))
        gem = by_model["gemini-2.0-flash"]
        self.assertEqual(gem["attributes"][c.GEN_AI_PROVIDER], "gemini")
        self.assertEqual(gem["attributes"][c.GEN_AI_INPUT_TOKENS], 2)
        self.assertEqual(gem["attributes"][c.GEN_AI_OUTPUT_TOKENS], 3)
        self.assertEqual(gem["attributes"][c.RA_COST_USD], cost_usd("gemini-2.0-flash", 2, 3))
        self.assertIn(f"key={REDACTED}", gem["attributes"]["url.path"])
        emb = by_model["text-embedding-004"]
        self.assertEqual(emb["attributes"][c.GEN_AI_OPERATION], "embeddings")
        self.assertEqual(emb["attributes"][c.GEN_AI_INPUT_TOKENS], 4)

    def test_traceparent_attaches_without_new_root(self):
        trace = "ab" * 16
        parent = "cd" * 8
        self.h.request("/openai/v1/chat/completions", {
            "model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}],
        }, headers={"traceparent": f"00-{trace}-{parent}-01"})
        spans = self.h.store.spans()
        self.assertEqual({s["trace_id"] for s in spans}, {trace})
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["parent_span_id"], parent)
        self.assertEqual(spans[0]["attributes"][c.GEN_AI_OPERATION], c.OP_CHAT)

    def test_unknown_path_is_not_an_open_proxy(self):
        status, _, body = self.h.request("/secret-internal/foo", {"a": 1})
        self.assertEqual(status, 404)
        self.assertEqual(self.h.fake.requests, [])
        self.assertIn(b"unknown path", body)

    def test_upstream_error_is_recorded(self):
        def fail(method, path, headers, body):
            raise ConnectionResetError("boom")
        # Swap handler to a close-the-connection style by shutting path: return 500 from a side
        # we simulate by pointing at a closed port via a one-off writer test of build_spans.
        spans = build_spans(
            "openai", "POST", "/v1/chat/completions", {},
            b'{"model":"gpt-4o-mini"}', 502, {}, b'{"error":"upstream error: timed out"}',
            1_000, 2_000, "proxy",
        )
        chat = next(s for s in spans if s["attributes"].get(c.GEN_AI_OPERATION) == c.OP_CHAT)
        self.assertEqual(chat["status_code"], 2)
        self.assertIn("502", chat["status_message"])

    def test_absolute_url_does_not_change_upstream_host(self):
        self.assertEqual(
            upstream_url("https://api.openai.com", "http://evil.example/v1/chat"),
            "https://api.openai.com/v1/chat",
        )
        self.assertEqual(
            upstream_url("https://api.openai.com", "//evil.example/v1/chat?key=1"),
            "https://api.openai.com/v1/chat?key=1",
        )

    def test_infer_routes(self):
        self.assertEqual(infer_route("/openai/v1/chat/completions", {})[0], "openai")
        self.assertEqual(infer_route("/v1/messages", {})[0], "anthropic")
        self.assertEqual(infer_route("/v1beta/models/g:generateContent", {})[0], "gemini")
        self.assertEqual(infer_route("/v1/embeddings", {})[0], "openai")
        self.assertIsNone(infer_route("/admin", {})[0])


if __name__ == "__main__":
    unittest.main()
