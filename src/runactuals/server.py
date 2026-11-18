"""OTLP/HTTP receiver plus dashboard and JSON API, on the stdlib HTTP server.

POST /v1/traces       OTLP/HTTP (protobuf or JSON, optionally gzip)
GET  /                dashboard
GET  /runs/<trace_id> one run
GET  /api/report      the four checks as JSON
GET  /healthz
"""

from __future__ import annotations

import dataclasses
import hmac
import json
import logging
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import dashboard
from .checks import build_report, run_cost
from .otlp import OTLPDecodeError, decode_request
from .store import Store

log = logging.getLogger("runactuals")
MAX_BODY = 16 * 1024 * 1024


def report_json(store: Store) -> dict:
    rep = build_report(store.spans(), store.latest_evidence())
    return {
        "summary": dataclasses.asdict(rep.cost) | {
            "false_failures": len(rep.false_failures), "false_successes": len(rep.false_successes)},
        "runs": [{
            "trace_id": r.trace_id, "agent": r.agent, "title": r.title, "start_ns": r.start_ns,
            "claimed": rep.outcomes[r.trace_id].claimed, "actual": rep.outcomes[r.trace_id].actual,
            "mismatch": rep.outcomes[r.trace_id].mismatch, "cost_usd": run_cost(r),
            "evidence": [{k: e[k] for k in ("adapter", "status", "summary", "url")}
                         for e in rep.outcomes[r.trace_id].evidence],
            "warnings": rep.warnings.get(r.trace_id, []),
        } for r in rep.runs],
        "audit": rep.audit,
    }


class RecorderServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, store: Store, forward: list[str] | None = None,
                 ingest_token: str | None = None):
        super().__init__(addr, Handler)
        self.store = store
        self.forward = forward or []
        self.ingest_token = ingest_token

    def fan_out(self, body: bytes, headers: dict) -> None:
        """Send the raw OTLP body on to DataCat, Grafana, or any OTLP/HTTP backend."""
        for url in self.forward:
            def send(url=url):
                try:
                    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
                    urllib.request.urlopen(req, timeout=10).read()
                except OSError as exc:
                    log.warning("forward to %s failed: %s", url, exc)
            threading.Thread(target=send, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server: RecorderServer
    server_version = "RunActuals"

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, code: int, body: bytes | str, ctype: str = "application/json") -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.split("?")[0] != "/v1/traces":
            return self._send(404, '{"error":"not found"}')
        token = self.server.ingest_token
        if token and not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
            return self._send(401, '{"error":"unauthorized"}')
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, '{"error":"body too large"}')
        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        try:
            spans = decode_request(body, ctype, self.headers.get("Content-Encoding", ""))
        except OTLPDecodeError as exc:
            return self._send(400, json.dumps({"error": str(exc)}))
        self.server.store.add_spans(spans)
        fwd = {"Content-Type": ctype}
        if self.headers.get("Content-Encoding"):
            fwd["Content-Encoding"] = self.headers["Content-Encoding"]
        self.server.fan_out(body, fwd)
        if ctype.startswith("application/json"):
            return self._send(200, "{}")
        return self._send(200, b"", "application/x-protobuf")  # empty ExportTraceServiceResponse

    def do_GET(self):
        path = self.path.split("?")[0]
        store = self.server.store
        if path == "/healthz":
            return self._send(200, '{"ok":true}')
        if path == "/api/report":
            return self._send(200, json.dumps(report_json(store), indent=1))
        if path == "/":
            rep = build_report(store.spans(), store.latest_evidence())
            return self._send(200, dashboard.render_index(rep), "text/html; charset=utf-8")
        if path.startswith("/runs/"):
            tid = path[len("/runs/"):]
            rep = build_report(store.spans(tid), store.latest_evidence())
            page = dashboard.render_run(rep, tid)
            if page:
                return self._send(200, page, "text/html; charset=utf-8")
        return self._send(404, '{"error":"not found"}')


def serve(store: Store, host: str = "127.0.0.1", port: int = 4318, **kwargs) -> RecorderServer:
    return RecorderServer((host, port), store, **kwargs)
