"""Decode OTLP/HTTP trace export requests (JSON or protobuf) with the stdlib.

Only the fields RunActuals needs are decoded. The protobuf reader is a small
wire-format walker over opentelemetry/proto/collector/trace/v1, so standard
OpenTelemetry exporters (which default to protobuf) work without extra
dependencies.
"""

from __future__ import annotations

import base64
import gzip
import json
import struct
from typing import Any, Iterator

STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2
_STATUS_NAMES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}


class OTLPDecodeError(ValueError):
    pass


def decode_request(body: bytes, content_type: str = "", content_encoding: str = "") -> list[dict]:
    """Return flat span dicts from an ExportTraceServiceRequest body."""
    if content_encoding.strip().lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except OSError as exc:
            raise OTLPDecodeError(f"bad gzip body: {exc}") from exc
    ctype = content_type.split(";")[0].strip().lower()
    if ctype == "application/json":
        return decode_json(body)
    if ctype in ("application/x-protobuf", "application/protobuf", ""):
        return decode_protobuf(body)
    raise OTLPDecodeError(f"unsupported content type: {content_type}")


# ---------------------------------------------------------------- JSON

def _json_any(v: dict | None) -> Any:
    if not v:
        return None
    if "stringValue" in v:
        return v["stringValue"]
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "arrayValue" in v:
        return [_json_any(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return _json_attrs(v["kvlistValue"].get("values", []))
    if "bytesValue" in v:
        return base64.b64decode(v["bytesValue"]).hex()
    return None


def _json_attrs(kvs: list | None) -> dict:
    return {kv["key"]: _json_any(kv.get("value")) for kv in (kvs or []) if "key" in kv}


def _status_code(code: Any) -> int:
    if isinstance(code, str):
        return _STATUS_NAMES.get(code, int(code) if code.isdigit() else 0)
    return int(code or 0)


def decode_json(body: bytes) -> list[dict]:
    try:
        req = json.loads(body or b"{}")
    except ValueError as exc:
        raise OTLPDecodeError(f"bad JSON: {exc}") from exc
    out = []
    for rs in req.get("resourceSpans", []):
        resource = _json_attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                status = s.get("status") or {}
                out.append({
                    "trace_id": (s.get("traceId") or "").lower(),
                    "span_id": (s.get("spanId") or "").lower(),
                    "parent_span_id": (s.get("parentSpanId") or "").lower(),
                    "name": s.get("name", ""),
                    "start_ns": int(s.get("startTimeUnixNano") or 0),
                    "end_ns": int(s.get("endTimeUnixNano") or 0),
                    "status_code": _status_code(status.get("code")),
                    "status_message": status.get("message", ""),
                    "attributes": _json_attrs(s.get("attributes")),
                    "events": [
                        {"name": e.get("name", ""), "time_ns": int(e.get("timeUnixNano") or 0),
                         "attributes": _json_attrs(e.get("attributes"))}
                        for e in s.get("events", [])
                    ],
                    "resource": resource,
                })
    _validate(out)
    return out


# ---------------------------------------------------------------- protobuf

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf):
            raise OTLPDecodeError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 70:
            raise OTLPDecodeError("varint too long")


def _fields(buf: bytes) -> Iterator[tuple[int, int, Any]]:
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _varint(buf, i)
        elif wire == 1:
            val, i = buf[i:i + 8], i + 8
        elif wire == 2:
            ln, i = _varint(buf, i)
            val, i = buf[i:i + ln], i + ln
        elif wire == 5:
            val, i = buf[i:i + 4], i + 4
        else:
            raise OTLPDecodeError(f"unsupported wire type {wire}")
        if i > n:
            raise OTLPDecodeError("truncated message")
        yield field, wire, val


def _pb_any(buf: bytes) -> Any:
    for f, _w, v in _fields(buf):
        if f == 1:
            return v.decode("utf-8", "replace")
        if f == 2:
            return bool(v)
        if f == 3:
            return v - (1 << 64) if v >= 1 << 63 else v
        if f == 4:
            return struct.unpack("<d", v)[0]
        if f == 5:
            return [_pb_any(x) for ff, _, x in _fields(v) if ff == 1]
        if f == 6:
            return dict(_pb_kv(x) for ff, _, x in _fields(v) if ff == 1)
        if f == 7:
            return bytes(v).hex()
    return None


def _pb_kv(buf: bytes) -> tuple[str, Any]:
    key, val = "", None
    for f, _w, v in _fields(buf):
        if f == 1:
            key = v.decode("utf-8", "replace")
        elif f == 2:
            val = _pb_any(v)
    return key, val


def _pb_span(buf: bytes, resource: dict) -> dict:
    s = {"trace_id": "", "span_id": "", "parent_span_id": "", "name": "", "start_ns": 0,
         "end_ns": 0, "status_code": 0, "status_message": "", "attributes": {}, "events": [],
         "resource": resource}
    for f, _w, v in _fields(buf):
        if f == 1:
            s["trace_id"] = bytes(v).hex()
        elif f == 2:
            s["span_id"] = bytes(v).hex()
        elif f == 4:
            s["parent_span_id"] = bytes(v).hex()
        elif f == 5:
            s["name"] = v.decode("utf-8", "replace")
        elif f == 7:
            s["start_ns"] = int.from_bytes(v, "little")
        elif f == 8:
            s["end_ns"] = int.from_bytes(v, "little")
        elif f == 9:
            k, val = _pb_kv(v)
            s["attributes"][k] = val
        elif f == 11:
            ev = {"name": "", "time_ns": 0, "attributes": {}}
            for ef, _, ev_v in _fields(v):
                if ef == 1:
                    ev["time_ns"] = int.from_bytes(ev_v, "little")
                elif ef == 2:
                    ev["name"] = ev_v.decode("utf-8", "replace")
                elif ef == 3:
                    k, val = _pb_kv(ev_v)
                    ev["attributes"][k] = val
            s["events"].append(ev)
        elif f == 15:
            for sf, _, sv in _fields(v):
                if sf == 2:
                    s["status_message"] = sv.decode("utf-8", "replace")
                elif sf == 3:
                    s["status_code"] = sv
    return s


def decode_protobuf(body: bytes) -> list[dict]:
    out = []
    try:
        for f, _w, rs in _fields(body):
            if f != 1:
                continue
            resource: dict = {}
            scope_spans = []
            for rf, _, rv in _fields(rs):
                if rf == 1:
                    resource = dict(_pb_kv(x) for af, _, x in _fields(rv) if af == 1)
                elif rf == 2:
                    scope_spans.append(rv)
            for ss in scope_spans:
                for sf, _, sv in _fields(ss):
                    if sf == 2:
                        out.append(_pb_span(sv, resource))
    except (IndexError, struct.error, UnicodeDecodeError) as exc:
        raise OTLPDecodeError(f"bad protobuf body: {exc}") from exc
    _validate(out)
    return out


def _validate(spans: list[dict]) -> None:
    for s in spans:
        if not s["trace_id"] or not s["span_id"]:
            raise OTLPDecodeError("span without traceId/spanId")
