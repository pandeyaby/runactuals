"""HTTP proxy that records LLM (and optional MCP) calls into the RunActuals store.

Point an SDK at this process instead of the provider:

    OPENAI_BASE_URL=http://127.0.0.1:8787/openai/v1
    ANTHROPIC_BASE_URL=http://127.0.0.1:8787/anthropic
    # Google GenAI: Client(http_options={"base_url": "http://127.0.0.1:8787/gemini"})

The proxy forwards to the real upstream, then writes a chat/embeddings span
(model, tokens, latency, price-table cost, redacted metadata). API keys are
forwarded upstream and stripped from everything that is stored.

No open-proxy behaviour: only configured upstreams are reachable, and only
through the documented path prefixes (plus a few unambiguous implicit paths).
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlparse

from . import conventions as c
from .mcp import record_mcp_http
from .prices import cost_usd
from .record import SpanWriter, session_from_headers, store_span, with_root
from .redact import redact_body, redact_headers, redact_path_and_query

log = logging.getLogger("runactuals.proxy")

DEFAULT_UPSTREAMS = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
}
PREFIXES = (("openai", "/openai"), ("anthropic", "/anthropic"),
            ("gemini", "/gemini"), ("mcp", "/mcp"))
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})
_GEMINI_MODEL = re.compile(r"/models/([^/:?]+)")
MAX_BODY = 32 * 1024 * 1024
PREVIEW_LIMIT = 8192


def _json(body: bytes | str | None) -> dict | None:
    if body is None:
        return None
    raw = body if isinstance(body, (bytes, bytearray)) else str(body).encode()
    raw = raw.strip()
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except ValueError:
        return None
    return val if isinstance(val, dict) else None


def sse_payloads(body: bytes) -> list[dict]:
    text = body.decode("utf-8", "replace")
    out: list[dict] = []
    for block in text.split("\n\n"):
        data_lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            continue
        try:
            val = json.loads(data)
        except ValueError:
            continue
        if isinstance(val, dict):
            out.append(val)
    return out


def _iter_json_dicts(body: bytes) -> list[dict]:
    obj = _json(body)
    if obj is not None:
        return [obj]
    payloads = sse_payloads(body)
    if payloads:
        return payloads
    # Gemini sometimes returns concatenated JSON values or a JSON array.
    text = body.decode("utf-8", "replace").strip()
    if text.startswith("["):
        try:
            arr = json.loads(text)
            return [x for x in arr if isinstance(x, dict)]
        except ValueError:
            pass
    out: list[dict] = []
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        try:
            val, end = dec.raw_decode(text, i)
        except ValueError:
            break
        if isinstance(val, dict):
            out.append(val)
        i = end
    return out


def _usage_from_mapping(u: dict | None) -> tuple[int, int]:
    if not u:
        return 0, 0
    inp = (u.get("prompt_tokens") or u.get("input_tokens") or u.get("promptTokenCount")
           or u.get("prompt_token_count") or 0)
    out = (u.get("completion_tokens") or u.get("output_tokens") or u.get("candidatesTokenCount")
           or u.get("candidates_token_count") or 0)
    return int(inp or 0), int(out or 0)


def extract_usage(payloads: list[dict]) -> tuple[int, int, str | None, str | None]:
    """Return (input_tokens, output_tokens, model, finish_reason) from provider payloads."""
    inp = out = 0
    model = finish = None
    for p in payloads:
        model = p.get("model") or model
        u = p.get("usage") or p.get("usageMetadata")
        if isinstance(u, dict):
            i, o = _usage_from_mapping(u)
            # Anthropic streams split usage across events; take max / last non-zero.
            if i:
                inp = i
            if o:
                out = o
        typ = p.get("type")
        if typ == "message_start" and isinstance(p.get("message"), dict):
            msg = p["message"]
            model = msg.get("model") or model
            i, o = _usage_from_mapping(msg.get("usage") if isinstance(msg.get("usage"), dict) else None)
            if i:
                inp = i
        if typ == "message_delta" and isinstance(p.get("usage"), dict):
            _, o = _usage_from_mapping(p["usage"])
            if o:
                out = o
        for choice in p.get("choices") or []:
            if isinstance(choice, dict) and choice.get("finish_reason"):
                finish = str(choice["finish_reason"])
        if p.get("stop_reason"):
            finish = str(p["stop_reason"])
        for cand in p.get("candidates") or []:
            if isinstance(cand, dict) and cand.get("finishReason"):
                finish = str(cand["finishReason"])
    return inp, out, str(model) if model else None, finish


def model_from_path(path: str) -> str | None:
    m = _GEMINI_MODEL.search(path)
    return m.group(1) if m else None


def operation_for(path: str) -> str:
    p = path.split("?")[0].lower()
    if any(s in p for s in ("embedcontent", "/embeddings", "batchembedcontents")):
        return "embeddings"
    if any(s in p for s in ("counttokens", "/moderations")):
        return "chat"
    return c.OP_CHAT


def infer_route(path: str, headers: dict[str, str]) -> tuple[str | None, str]:
    """Return (provider, upstream_path_and_query)."""
    path_only = path.split("?")[0]
    for provider, prefix in PREFIXES:
        if path_only == prefix or path_only.startswith(prefix + "/"):
            rest = path[len(prefix):] or "/"
            if not rest.startswith(("/", "?")):
                rest = "/" + rest
            if rest.startswith("?"):
                rest = "/" + rest
            return provider, rest
    lower_headers = {k.lower(): v for k, v in headers.items()}
    if path_only.startswith("/v1/messages") or "anthropic-version" in lower_headers:
        return "anthropic", path
    if "/models/" in path_only and ":" in path_only.split("?")[0]:
        return "gemini", path
    if path_only.startswith("/v1/") or path_only == "/v1":
        return "openai", path
    return None, path


def upstream_url(origin: str, path: str) -> str:
    """Join a configured upstream origin with the client path. The client host is ignored."""
    u = urlparse(origin)
    target = urlparse(path)
    raw_path = target.path or "/"
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path
    query = target.query
    return f"{u.scheme}://{u.netloc}{raw_path}" + (f"?{query}" if query else "")


def _target_path(path: str) -> str:
    p = urlparse(path)
    out = p.path or "/"
    if not out.startswith("/"):
        out = "/" + out
    return out + (f"?{p.query}" if p.query else "")


class ProxyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _forward_headers(headers: dict[str, str], host: str) -> dict[str, str]:
    out = {}
    for k, v in headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        out[k] = v
    out["Host"] = host
    return out


def call_upstream(method: str, origin: str, path: str, headers: dict[str, str],
                  body: bytes, timeout: float) -> tuple[int, dict[str, str], bytes]:
    u = urlparse(origin)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ProxyError(500, "misconfigured upstream")
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(u.netloc, timeout=timeout)
    try:
        conn.request(method, _target_path(path), body=body or None,
                     headers=_forward_headers(headers, u.netloc))
        resp = conn.getresponse()
        data = resp.read()
        rh = {k: v for k, v in resp.getheaders()}
        return resp.status, rh, data
    except OSError as exc:
        raise ProxyError(502, f"upstream error: {exc}") from exc
    finally:
        conn.close()


def build_spans(provider: str, method: str, path: str, request_headers: dict[str, str],
                request_body: bytes, status: int, response_headers: dict[str, str],
                response_body: bytes, start_ns: int, end_ns: int,
                default_agent: str, resource: dict | None = None) -> list[dict]:
    req = _json(request_body) or {}
    payloads = _iter_json_dicts(response_body)
    inp, out, resp_model, finish = extract_usage(payloads)
    model = (req.get("model") or resp_model or model_from_path(path) or "")
    if isinstance(model, str) and model.startswith("models/"):
        model = model.split("/", 1)[-1]
    operation = operation_for(path)
    usd = cost_usd(model, inp, out)
    error = None if 200 <= status < 400 else f"HTTP {status}"
    if not error and response_body:
        # Gemini and others may return 200 with an error object.
        first = payloads[0] if payloads else _json(response_body)
        if isinstance(first, dict) and isinstance(first.get("error"), dict):
            err = first["error"]
            error = str(err.get("message") or err.get("status") or "upstream error")
    attrs: dict = {
        c.GEN_AI_OPERATION: operation,
        c.GEN_AI_PROVIDER: provider,
        "http.request.method": method,
        "url.path": redact_path_and_query(path),
        "http.response.status_code": status,
        "server.address": provider,
    }
    if model:
        attrs[c.GEN_AI_REQUEST_MODEL] = model
    if inp:
        attrs[c.GEN_AI_INPUT_TOKENS] = inp
    if out:
        attrs[c.GEN_AI_OUTPUT_TOKENS] = out
    if usd is not None:
        attrs[c.RA_COST_USD] = usd
    elif model:
        attrs["runactuals.cost.unknown_model"] = True
    if finish:
        attrs[c.GEN_AI_FINISH_REASONS] = finish
    preview_req = redact_body(request_body, PREVIEW_LIMIT)
    preview_resp = redact_body(response_body, PREVIEW_LIMIT)
    if preview_req:
        attrs["runactuals.proxy.request_preview"] = preview_req
    if preview_resp:
        attrs["runactuals.proxy.response_preview"] = preview_resp
    # Record that headers were seen, with secrets stripped (helps debugging auth setup).
    attrs["runactuals.proxy.request_headers"] = json.dumps(redact_headers(request_headers), sort_keys=True)
    latency_ms = max(0.0, (end_ns - start_ns) / 1e6)
    attrs["runactuals.proxy.latency_ms"] = round(latency_ms, 3)

    trace_id, span_id, parent, agent = session_from_headers(request_headers, default_agent)
    title = f"{provider} {operation}" + (f" {model}" if model else "")
    spans, parent_id = with_root(trace_id, parent, agent, start_ns, end_ns, title,
                                 error=error, cost_usd=usd, resource=resource)
    name = f"{operation} {model}".strip() if model else f"{operation} {provider}"
    spans.append(store_span(
        trace_id, span_id, name, start_ns, end_ns, attributes=attrs,
        parent_span_id=parent_id, error=error, resource=resource,
    ))
    return spans


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, writer: SpanWriter, upstreams: dict[str, str] | None = None,
                 agent: str = "proxy", timeout: float = 300.0,
                 clock: Callable[[], int] = time.time_ns):
        super().__init__(addr, ProxyHandler)
        self.writer = writer
        self.upstreams = {**DEFAULT_UPSTREAMS, **(upstreams or {})}
        self.agent = agent
        self.timeout = timeout
        self.clock = clock


class ProxyHandler(BaseHTTPRequestHandler):
    server: ProxyServer
    server_version = "RunActualsProxy"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        safe = tuple(redact_path_and_query(a) if isinstance(a, str) else a for a in args)
        log.debug("%s " + fmt, self.address_string(), *safe)

    def _send(self, code: int, body: bytes | str, ctype: str = "application/json",
              extra: dict[str, str] | None = None) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(413, '{"error":"body too large"}')
            return None
        return self.rfile.read(length) if length else b""

    def _headers(self) -> dict[str, str]:
        return {k: v for k, v in self.headers.items()}

    def do_GET(self):
        if self.path.split("?")[0] == "/healthz":
            return self._send(200, '{"ok":true,"role":"proxy"}')
        return self._proxy("GET")

    def do_POST(self):
        return self._proxy("POST")

    def do_PUT(self):
        return self._proxy("PUT")

    def do_PATCH(self):
        return self._proxy("PATCH")

    def do_DELETE(self):
        return self._proxy("DELETE")

    def do_OPTIONS(self):
        if self.path.split("?")[0] == "/healthz":
            return self._send(200, b"", extra={"Allow": "GET"})
        return self._proxy("OPTIONS")

    def _proxy(self, method: str) -> None:
        body = self._read_body()
        if body is None:
            return
        headers = self._headers()
        provider, up_path = infer_route(self.path, headers)
        if not provider:
            return self._send(404, json.dumps({
                "error": "unknown path; use /openai/..., /anthropic/..., /gemini/..., or /mcp/..."
            }))
        origin = self.server.upstreams.get(provider)
        if not origin:
            return self._send(502, json.dumps({"error": f"no upstream configured for {provider}"}))
        start_ns = self.server.clock()
        try:
            status, resp_headers, resp_body = call_upstream(
                method, origin, up_path, headers, body, self.server.timeout)
        except ProxyError as exc:
            status, resp_headers, resp_body = exc.status, {"Content-Type": "application/json"}, (
                json.dumps({"error": exc.message}).encode())
        end_ns = self.server.clock()
        try:
            if provider == "mcp":
                spans = record_mcp_http(
                    method, up_path, headers, body, status, resp_headers, resp_body,
                    start_ns, end_ns, self.server.agent)
            else:
                spans = build_spans(
                    provider, method, up_path, headers, body, status, resp_headers, resp_body,
                    start_ns, end_ns, self.server.agent)
            self.server.writer.write(spans)
        except Exception:
            log.exception("failed to record %s %s", method, redact_path_and_query(self.path))
        ctype = next((v for k, v in resp_headers.items() if k.lower() == "content-type"),
                     "application/json")
        extra = {}
        for k, v in resp_headers.items():
            if k.lower() in HOP_BY_HOP or k.lower() in {"content-type", "content-length", "connection"}:
                continue
            extra[k] = v
        self._send(status, resp_body, ctype, extra)


def serve_proxy(writer: SpanWriter, host: str = "127.0.0.1", port: int = 8787,
                **kwargs) -> ProxyServer:
    return ProxyServer((host, port), writer, **kwargs)
