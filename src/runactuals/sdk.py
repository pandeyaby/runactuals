"""Thin SDK: record one agent run as OpenTelemetry GenAI spans and ship them as OTLP/JSON.

    from runactuals.sdk import Recorder

    rec = Recorder("http://localhost:4318", service_name="lesson-builder")
    with rec.run("lesson-builder", title="Build: Telescopes") as run:
        run.llm("grok-4", input_tokens=1200, output_tokens=800, cost_usd=0.004)
        run.tool("git_push", {"branch": "visual-draft"})
        run.claim("git_commit", branch="visual-draft", revision=sha)
        run.audit("write", "github:pandeyaby/why-next@visual-draft", approval_ref="standing rule")
    # leaving the block ends the run as "succeeded"; an exception ends it as "failed".

No dependencies. If you already use the OpenTelemetry SDK, emit the same
attributes (see runactuals.conventions) on your own spans instead.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.request
from typing import Any, Callable

from . import __version__
from . import conventions as c
from .otlp import STATUS_ERROR, STATUS_OK


def _any(v: Any) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [_any(x) for x in v]}}
    if isinstance(v, dict):
        return {"kvlistValue": {"values": _attrs(v)}}
    return {"stringValue": str(v)}


def _attrs(d: dict) -> list[dict]:
    return [{"key": k, "value": _any(v)} for k, v in d.items() if v is not None]


class Recorder:
    def __init__(self, endpoint: str | None = None, service_name: str = "agent",
                 clock: Callable[[], int] = time.time_ns, headers: dict | None = None,
                 resource: dict | None = None):
        self.endpoint = (endpoint or os.environ.get("RUNACTUALS_ENDPOINT")
                         or "http://localhost:4318").rstrip("/")
        self.clock = clock
        self.headers = dict(headers or {})
        token = os.environ.get("RUNACTUALS_INGEST_TOKEN")
        if token and "Authorization" not in self.headers:
            self.headers["Authorization"] = f"Bearer {token}"
        self.resource = {"service.name": service_name, **(resource or {})}

    def run(self, agent: str, title: str | None = None, **attributes) -> "RunRecorder":
        return RunRecorder(self, agent, title, attributes)

    def export(self, spans: list[dict]) -> None:
        body = json.dumps({"resourceSpans": [{
            "resource": {"attributes": _attrs(self.resource)},
            "scopeSpans": [{"scope": {"name": "runactuals.sdk", "version": __version__},
                            "spans": spans}]}]}).encode()
        req = urllib.request.Request(self.endpoint + "/v1/traces", data=body, method="POST",
                                     headers={"Content-Type": "application/json", **self.headers})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()


class RunRecorder:
    def __init__(self, rec: Recorder, agent: str, title: str | None, attributes: dict):
        self.rec = rec
        self.agent = agent
        self.trace_id = secrets.token_hex(16)
        self.span_id = secrets.token_hex(8)
        self.start_ns = rec.clock()
        self.attributes = {c.GEN_AI_OPERATION: c.OP_INVOKE_AGENT, c.GEN_AI_AGENT_NAME: agent,
                           "runactuals.run.title": title or f"invoke_agent {agent}", **attributes}
        self._spans: list[dict] = []
        self._ended = False

    def _span(self, name: str, attrs: dict, duration_ms: float = 0, error: str | None = None) -> None:
        end = self.rec.clock()
        start = end - int(duration_ms * 1e6)
        status = {"code": STATUS_ERROR, "message": error} if error else {"code": 0}
        self._spans.append({
            "traceId": self.trace_id, "spanId": secrets.token_hex(8), "parentSpanId": self.span_id,
            "name": name, "kind": 1, "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
            "attributes": _attrs(attrs), "status": status})

    def llm(self, model: str, input_tokens: int = 0, output_tokens: int = 0,
            cost_usd: float | None = None, duration_ms: float = 0) -> None:
        self._span(f"{c.OP_CHAT} {model}", {
            c.GEN_AI_OPERATION: c.OP_CHAT, c.GEN_AI_REQUEST_MODEL: model,
            c.GEN_AI_INPUT_TOKENS: input_tokens, c.GEN_AI_OUTPUT_TOKENS: output_tokens,
            c.RA_COST_USD: cost_usd}, duration_ms)

    def tool(self, name: str, arguments: Any = None, error: str | None = None,
             duration_ms: float = 0, retry: bool = False) -> None:
        args = arguments if arguments is None or isinstance(arguments, str) else json.dumps(arguments, sort_keys=True)
        self._span(f"{c.OP_EXECUTE_TOOL} {name}", {
            c.GEN_AI_OPERATION: c.OP_EXECUTE_TOOL, c.GEN_AI_TOOL_NAME: name,
            c.GEN_AI_TOOL_CALL_ARGS: args, c.RA_RETRY: retry or None}, duration_ms, error)

    def claim(self, kind: str, summary: str | None = None, branch: str | None = None,
              revision: str | None = None, **attrs) -> None:
        """Something the agent says it achieved. Adapters check it; nothing trusts it."""
        self._span(c.SPAN_CLAIM, {c.RA_CLAIM_KIND: kind, c.RA_CLAIM_SUMMARY: summary,
                                  c.VCS_REF_HEAD_NAME: branch, c.VCS_REF_HEAD_REVISION: revision,
                                  **{f"runactuals.claim.{k}": v for k, v in attrs.items()}})

    def audit(self, action: str, target: str, actor: str | None = None,
              approval_ref: str | None = None, spend_usd: float | None = None) -> None:
        """Record an action that touched the world: approval, send, write, spend, post, delete."""
        self._span(c.SPAN_AUDIT, {c.RA_AUDIT_ACTION: action, c.RA_AUDIT_TARGET: target,
                                  c.RA_AUDIT_ACTOR: actor, c.RA_AUDIT_APPROVAL: approval_ref,
                                  c.RA_SPEND_USD: spend_usd})

    def quality(self, score: float) -> None:
        self.attributes[c.RA_QUALITY] = float(score)

    def end(self, claimed_status: str = c.CLAIMED_SUCCEEDED, message: str | None = None) -> None:
        if self._ended:
            return
        self._ended = True
        self.attributes[c.RA_CLAIMED_STATUS] = claimed_status
        if message:
            self.attributes[c.RA_STATUS_MESSAGE] = message
        ok = claimed_status == c.CLAIMED_SUCCEEDED
        root = {"traceId": self.trace_id, "spanId": self.span_id, "name": f"invoke_agent {self.agent}",
                "kind": 1, "startTimeUnixNano": str(self.start_ns),
                "endTimeUnixNano": str(self.rec.clock()), "attributes": _attrs(self.attributes),
                "status": {"code": STATUS_OK} if ok else {"code": STATUS_ERROR, "message": message or claimed_status}}
        self.rec.export([root] + self._spans)

    def __enter__(self) -> "RunRecorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            status = c.CLAIMED_INTERRUPTED if isinstance(exc, (KeyboardInterrupt, SystemExit)) else c.CLAIMED_FAILED
            self.end(status, f"{exc_type.__name__}: {exc}")
        else:
            self.end()
        return False
