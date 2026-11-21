"""Write store-format spans to SQLite and/or the OTLP collector."""

from __future__ import annotations

import secrets
from typing import Any

from .otlp import STATUS_ERROR, STATUS_OK
from .sdk import Recorder, _attrs
from .store import Store


def new_ids() -> tuple[str, str]:
    return secrets.token_hex(16), secrets.token_hex(8)


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """Parse a W3C ``traceparent`` header. Returns (trace_id, parent_span_id) or None."""
    if not value:
        return None
    parts = value.strip().split("-")
    if len(parts) < 3:
        return None
    _ver, trace_id, parent_id = parts[0], parts[1], parts[2]
    if len(trace_id) != 32 or len(parent_id) != 16:
        return None
    try:
        int(trace_id, 16)
        int(parent_id, 16)
    except ValueError:
        return None
    if trace_id == "0" * 32 or parent_id == "0" * 16:
        return None
    return trace_id.lower(), parent_id.lower()


def store_span(trace_id: str, span_id: str, name: str, start_ns: int, end_ns: int,
               attributes: dict | None = None, parent_span_id: str = "",
               error: str | None = None, resource: dict | None = None,
               events: list | None = None) -> dict:
    return {
        "trace_id": trace_id, "span_id": span_id, "parent_span_id": parent_span_id or "",
        "name": name, "start_ns": int(start_ns), "end_ns": int(end_ns),
        "status_code": STATUS_ERROR if error else STATUS_OK,
        "status_message": error or "",
        "attributes": dict(attributes or {}),
        "events": list(events or []),
        "resource": dict(resource or {}),
    }


def to_otlp_span(span: dict) -> dict:
    status: dict[str, Any] = {"code": span.get("status_code") or 0}
    if span.get("status_message"):
        status["message"] = span["status_message"]
    out = {
        "traceId": span["trace_id"], "spanId": span["span_id"], "name": span.get("name", ""),
        "kind": 1, "startTimeUnixNano": str(span.get("start_ns", 0)),
        "endTimeUnixNano": str(span.get("end_ns", 0)),
        "attributes": _attrs(span.get("attributes") or {}), "status": status,
    }
    if span.get("parent_span_id"):
        out["parentSpanId"] = span["parent_span_id"]
    if span.get("events"):
        out["events"] = [{"name": e.get("name", ""), "timeUnixNano": str(e.get("time_ns", 0)),
                          "attributes": _attrs(e.get("attributes") or {})} for e in span["events"]]
    return out


class SpanWriter:
    """Emit spans to a Store, an OTLP endpoint, or both."""

    def __init__(self, store: Store | None = None, endpoint: str | None = None,
                 service_name: str = "runactuals-proxy", headers: dict | None = None,
                 resource: dict | None = None):
        self.store = store
        self.resource = {"service.name": service_name, **(resource or {})}
        self.recorder = Recorder(endpoint, service_name=service_name,
                                 headers=headers, resource=resource) if endpoint else None

    def write(self, spans: list[dict]) -> None:
        if not spans:
            return
        filled = []
        for s in spans:
            item = dict(s)
            item.setdefault("resource", dict(self.resource))
            if self.resource:
                item["resource"] = {**self.resource, **(s.get("resource") or {})}
            filled.append(item)
        if self.store is not None:
            self.store.add_spans(filled)
        if self.recorder is not None:
            self.recorder.export([to_otlp_span(s) for s in filled])


def session_from_headers(headers: dict[str, str], default_agent: str) -> tuple[str, str, str | None, str]:
    """Return (trace_id, span_id, parent_or_None_to_mint_root, agent) from request headers."""
    def header(name: str) -> str | None:
        want = name.lower()
        for k, v in headers.items():
            if k.lower() == want:
                return v
        return None

    agent = (header("x-runactuals-agent") or default_agent).strip() or default_agent
    linked = parse_traceparent(header("traceparent"))
    _, span_id = new_ids()
    if linked:
        return linked[0], span_id, linked[1], agent
    explicit = header("x-runactuals-trace-id")
    parent = header("x-runactuals-parent-span-id")
    if explicit and len(explicit.strip()) == 32:
        try:
            int(explicit.strip(), 16)
        except ValueError:
            pass
        else:
            p = (parent or "").strip().lower()
            return explicit.strip().lower(), span_id, (p or None), agent
    trace_id, _ = new_ids()
    return trace_id, span_id, None, agent


def with_root(trace_id: str, parent: str | None, agent: str, start_ns: int, end_ns: int,
              title: str, error: str | None = None, cost_usd: float | None = None,
              resource: dict | None = None) -> tuple[list[dict], str]:
    """Mint an invoke_agent root when this call starts a new trace."""
    from . import conventions as c
    if parent is not None:
        return [], parent
    root_id = new_ids()[1]
    attrs = {
        c.GEN_AI_OPERATION: c.OP_INVOKE_AGENT, c.GEN_AI_AGENT_NAME: agent,
        "runactuals.run.title": title,
        c.RA_CLAIMED_STATUS: c.CLAIMED_FAILED if error else c.CLAIMED_SUCCEEDED,
    }
    if cost_usd is not None:
        attrs[c.RA_COST_USD] = cost_usd
    root = store_span(trace_id, root_id, f"{c.OP_INVOKE_AGENT} {agent}", start_ns, end_ns,
                      attributes=attrs, error=error, resource=resource)
    return [root], root_id
