"""Small builders for tests, plus a minimal protobuf encoder for OTLP."""

from __future__ import annotations

import itertools
import struct

from runactuals import conventions as c

_ids = itertools.count(1)


def span(trace="t1", name="s", parent="root", start=0, end=0, status=0, **attrs):
    return {"trace_id": trace, "span_id": f"{next(_ids):016x}", "parent_span_id": parent,
            "name": name, "start_ns": start, "end_ns": end, "status_code": status,
            "status_message": "", "attributes": attrs, "events": [], "resource": {}}


def root(trace="t1", agent="bot", claimed="succeeded", start=0, end=10**9, **attrs):
    s = span(trace, f"invoke_agent {agent}", "", start, end, **{
        c.GEN_AI_OPERATION: c.OP_INVOKE_AGENT, c.GEN_AI_AGENT_NAME: agent,
        c.RA_CLAIMED_STATUS: claimed, **attrs})
    s["span_id"] = "root"
    return s


def tool(trace="t1", name="t", args="{}", start=0, status=0, **attrs):
    return span(trace, f"execute_tool {name}", start=start, end=start, status=status, **{
        c.GEN_AI_OPERATION: c.OP_EXECUTE_TOOL, c.GEN_AI_TOOL_NAME: name,
        c.GEN_AI_TOOL_CALL_ARGS: args, **attrs})


# --- protobuf encoding (enough for ExportTraceServiceRequest)

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def ld(field: int, payload: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def vint(field: int, n: int) -> bytes:
    return _varint(field << 3) + _varint(n & (2**64 - 1))


def fixed64(field: int, n: int) -> bytes:
    return _varint(field << 3 | 1) + struct.pack("<Q", n)


def kv(key: str, value) -> bytes:
    if isinstance(value, bool):
        anyv = vint(2, int(value))
    elif isinstance(value, int):
        anyv = vint(3, value)
    elif isinstance(value, float):
        anyv = _varint(4 << 3 | 1) + struct.pack("<d", value)
    else:
        anyv = ld(1, str(value).encode())
    return ld(1, key.encode()) + ld(2, anyv)


def pb_request(trace_id: bytes, span_id: bytes, name: str, start: int, end: int, attrs: dict,
               status_code: int = 0, parent: bytes = b"", service="svc") -> bytes:
    sp = ld(1, trace_id) + ld(2, span_id) + (ld(4, parent) if parent else b"") + ld(5, name.encode())
    sp += vint(6, 1) + fixed64(7, start) + fixed64(8, end)
    for k, v in attrs.items():
        sp += ld(9, kv(k, v))
    sp += ld(15, ld(2, b"msg") + vint(3, status_code))
    resource = ld(1, kv("service.name", service))
    scope_spans = ld(1, ld(1, b"scope")) + ld(2, sp)
    return ld(1, ld(1, resource) + ld(2, scope_spans))
