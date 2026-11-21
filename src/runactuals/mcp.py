"""Record MCP JSON-RPC traffic (streamable HTTP and stdio wrapper).

The HTTP path is served by ``runactuals proxy`` at ``/mcp/...`` when
``--mcp-upstream`` is set. The stdio wrapper is ``runactuals wrap -- cmd``.

JSON-RPC ``tools/call`` becomes an ``execute_tool`` span; other methods are
recorded as ``mcp.<method>`` spans. Arguments and results are redacted before
storage. API keys never appear in recorded data.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from typing import Any, Callable, IO

from . import conventions as c
from .record import SpanWriter, session_from_headers, store_span, with_root, new_ids
from .redact import redact_body, redact_headers, redact_path_and_query

log = logging.getLogger("runactuals.mcp")

# MCP (and LSP) stdio framing, plus newline-delimited JSON as a fallback.
_HEADER_END = b"\r\n\r\n"


def parse_jsonrpc(raw: bytes | str | None) -> list[dict]:
    """Extract JSON-RPC objects from a body that may be JSON, NDJSON, or SSE."""
    if raw is None:
        return []
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    text = text.strip()
    if not text:
        return []
    out: list[dict] = []

    def _add(val: Any) -> None:
        if isinstance(val, dict) and ("jsonrpc" in val or "method" in val or "id" in val):
            out.append(val)
        elif isinstance(val, list):
            for item in val:
                _add(item)

    try:
        _add(json.loads(text))
        if out:
            return out
    except ValueError:
        pass
    # SSE
    for block in text.split("\n\n"):
        data_lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
        data = "\n".join(data_lines).strip()
        if not data:
            continue
        try:
            _add(json.loads(data))
        except ValueError:
            continue
    if out:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            _add(json.loads(line))
        except ValueError:
            continue
    return out


def _error_message(msg: dict) -> str | None:
    err = msg.get("error")
    if not isinstance(err, dict):
        return None
    if err.get("message"):
        return str(err["message"])
    if err.get("code") is not None:
        return f"jsonrpc error {err['code']}"
    return "jsonrpc error"


def _pair_responses(requests: list[dict], responses: list[dict]) -> dict[Any, dict]:
    by_id = {}
    for r in responses:
        if "id" in r:
            by_id[r["id"]] = r
    return by_id


def spans_for_messages(requests: list[dict], responses: list[dict],
                       start_ns: int, end_ns: int, agent: str,
                       headers: dict[str, str] | None = None,
                       path: str = "", method: str = "POST",
                       http_status: int | None = None,
                       resource: dict | None = None) -> list[dict]:
    """Build store spans for a batch of JSON-RPC messages."""
    if not requests and not responses:
        # Notification-only or empty: still record a bare span if HTTP happened.
        requests = [{"method": "mcp.http", "id": None}]
    by_id = _pair_responses(requests, responses)
    # Notifications have no id; responses without a matching request are recorded alone.
    pending_ids = {r["id"] for r in requests if "id" in r}
    extras = [r for r in responses if "id" in r and r["id"] not in pending_ids]

    headers = headers or {}
    first_trace = session_from_headers(headers, agent)
    trace_id, _, parent, agent = first_trace
    http_err = None if http_status is None or 200 <= http_status < 400 else f"HTTP {http_status}"
    # Cost is unused for MCP; leave unset.
    title = "mcp"
    methods = [str(r.get("method") or "rpc") for r in requests if r.get("method")]
    if len(methods) == 1:
        title = f"mcp {methods[0]}"
    elif methods:
        title = f"mcp {len(methods)} calls"
    roots, parent_id = with_root(trace_id, parent, agent, start_ns, end_ns, title,
                                 error=http_err, resource=resource)
    spans = list(roots)
    common = {
        "http.request.method": method or None,
        "url.path": redact_path_and_query(path) if path else None,
        "runactuals.proxy.request_headers": json.dumps(redact_headers(headers), sort_keys=True) if headers else None,
    }
    if http_status is not None:
        common["http.response.status_code"] = http_status
    common = {k: v for k, v in common.items() if v is not None}

    def one(req: dict | None, resp: dict | None) -> dict:
        rpc_method = str((req or {}).get("method") or (resp or {}).get("method") or "rpc")
        params = (req or {}).get("params") if isinstance((req or {}).get("params"), dict) else {}
        tool = params.get("name") if rpc_method == "tools/call" else None
        if rpc_method == "tools/call" and tool:
            name = f"{c.OP_EXECUTE_TOOL} {tool}"
            attrs = {c.GEN_AI_OPERATION: c.OP_EXECUTE_TOOL, c.GEN_AI_TOOL_NAME: str(tool), **common}
            args = params.get("arguments")
            if args is not None:
                raw = args if isinstance(args, str) else json.dumps(args, default=str)
                attrs[c.GEN_AI_TOOL_CALL_ARGS] = redact_body(raw, 4096)
        else:
            name = f"mcp {rpc_method}"
            attrs = {c.GEN_AI_OPERATION: "mcp", "rpc.method": rpc_method, **common}
            if params:
                attrs[c.GEN_AI_TOOL_CALL_ARGS] = redact_body(json.dumps(params, default=str), 4096)
        result = (resp or {}).get("result")
        if result is not None:
            preview = result if isinstance(result, str) else json.dumps(result, default=str)
            attrs["runactuals.proxy.response_preview"] = redact_body(preview, 4096)
        err = _error_message(resp or {}) or http_err
        if req and req.get("id") is not None:
            attrs["rpc.id"] = req["id"] if not isinstance(req["id"], (dict, list)) else json.dumps(req["id"])
        _, span_id = new_ids()
        return store_span(trace_id, span_id, name, start_ns, end_ns, attributes=attrs,
                          parent_span_id=parent_id, error=err, resource=resource)

    for req in requests:
        resp = by_id.get(req["id"]) if "id" in req else None
        # skip the synthetic placeholder if we invented it and there were no real messages
        spans.append(one(req, resp))
    for resp in extras:
        spans.append(one(None, resp))
    return spans


def record_mcp_http(method: str, path: str, request_headers: dict[str, str],
                    request_body: bytes, status: int, response_headers: dict[str, str],
                    response_body: bytes, start_ns: int, end_ns: int,
                    default_agent: str, resource: dict | None = None) -> list[dict]:
    requests = [m for m in parse_jsonrpc(request_body) if "method" in m]
    responses = [m for m in parse_jsonrpc(response_body) if "method" not in m or "result" in m or "error" in m]
    # SSE responses are results; requests coming back as notifications may include method
    responses = [m for m in parse_jsonrpc(response_body) if "result" in m or "error" in m]
    if not requests and not responses:
        requests = [{"method": f"{method} {path.split('?')[0]}"}]
    return spans_for_messages(requests, responses, start_ns, end_ns, default_agent,
                              headers=request_headers, path=path, method=method,
                              http_status=status, resource=resource)


# --------------------------------------------------------------------------- stdio framing

def encode_message(obj: dict) -> bytes:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload


class FrameDecoder:
    """Accumulate bytes and yield complete JSON-RPC objects (Content-Length or NDJSON)."""

    def __init__(self):
        self._buf = bytearray()
        self._mode: str | None = None  # "lsp" | "ndjson"

    def feed(self, data: bytes) -> list[dict]:
        self._buf.extend(data)
        out: list[dict] = []
        while True:
            msg = self._pop()
            if msg is None:
                break
            out.append(msg)
        return out

    def _pop(self) -> dict | None:
        if not self._buf:
            return None
        if self._mode is None:
            # Detect framing from the first bytes.
            if self._buf.startswith(b"Content-Length:") or self._buf.startswith(b"content-length:"):
                self._mode = "lsp"
            elif b"\n" in self._buf:
                # If we see a header-style start later, still allow detection after whitespace
                stripped = bytes(self._buf).lstrip()
                if stripped.lower().startswith(b"content-length:"):
                    self._mode = "lsp"
                    self._buf = bytearray(stripped)
                else:
                    self._mode = "ndjson"
            else:
                return None
        if self._mode == "lsp":
            return self._pop_lsp()
        return self._pop_ndjson()

    def _pop_lsp(self) -> dict | None:
        sep = self._buf.find(_HEADER_END)
        if sep < 0:
            return None
        header = bytes(self._buf[:sep]).decode("ascii", "replace")
        length = None
        for line in header.split("\r\n"):
            if line.lower().startswith("content-length:"):
                try:
                    length = int(line.split(":", 1)[1].strip())
                except ValueError:
                    length = None
        if length is None:
            # Malformed; drop the header and keep going
            del self._buf[:sep + len(_HEADER_END)]
            return None
        start = sep + len(_HEADER_END)
        if len(self._buf) < start + length:
            return None
        payload = bytes(self._buf[start:start + length])
        del self._buf[:start + length]
        try:
            val = json.loads(payload.decode("utf-8"))
        except ValueError:
            return None
        return val if isinstance(val, dict) else None

    def _pop_ndjson(self) -> dict | None:
        nl = self._buf.find(b"\n")
        if nl < 0:
            return None
        line = bytes(self._buf[:nl]).strip()
        del self._buf[:nl + 1]
        if not line:
            return None
        try:
            val = json.loads(line.decode("utf-8"))
        except ValueError:
            return None
        return val if isinstance(val, dict) else None


def _record_pair(writer: SpanWriter, pending: dict[Any, tuple[dict, int]],
                 incoming: dict, start_ns: int | None, agent: str, clock: Callable[[], int]) -> None:
    end_ns = clock()
    if "method" in incoming and "result" not in incoming and "error" not in incoming:
        if "id" in incoming:
            pending[incoming["id"]] = (incoming, start_ns or end_ns)
        else:
            writer.write(spans_for_messages([incoming], [], start_ns or end_ns, end_ns, agent))
        return
    # response
    start = start_ns or end_ns
    req = None
    if "id" in incoming and incoming["id"] in pending:
        req, start = pending.pop(incoming["id"])
    writer.write(spans_for_messages([req] if req else [], [incoming], start, end_ns, agent))


def wrap_stdio(command: list[str], writer: SpanWriter, agent: str = "mcp",
               clock: Callable[[], int] = time.time_ns,
               stdin: IO[bytes] | None = None, stdout: IO[bytes] | None = None,
               stderr: IO[bytes] | None = None) -> int:
    """Proxy a child MCP server on stdio and record JSON-RPC messages. Returns the exit code."""
    if not command:
        raise ValueError("wrap needs a command to run")
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin and proc.stdout and proc.stderr
    src_in = stdin or sys.stdin.buffer
    src_out = stdout or sys.stdout.buffer
    src_err = stderr or sys.stderr.buffer
    pending: dict[Any, tuple[dict, int]] = {}
    lock = threading.Lock()
    client_dec, server_dec = FrameDecoder(), FrameDecoder()

    def record(msg: dict, is_from_client: bool, t0: int) -> None:
        with lock:
            if is_from_client:
                _record_pair(writer, pending, msg, t0, agent, clock)
            else:
                _record_pair(writer, pending, msg, t0, agent, clock)

    def pump_client() -> None:
        try:
            while True:
                chunk = src_in.read(4096)
                if not chunk:
                    break
                t0 = clock()
                try:
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
                except BrokenPipeError:
                    break
                for msg in client_dec.feed(chunk):
                    record(msg, True, t0)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def pump_server() -> None:
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                t0 = clock()
                src_out.write(chunk)
                src_out.flush()
                for msg in server_dec.feed(chunk):
                    record(msg, False, t0)
        except BrokenPipeError:
            pass

    def pump_stderr() -> None:
        try:
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    break
                src_err.write(chunk)
                src_err.flush()
        except BrokenPipeError:
            pass

    threads = [threading.Thread(target=fn, daemon=True)
               for fn in (pump_client, pump_server, pump_stderr)]
    for t in threads:
        t.start()
    try:
        return proc.wait()
    finally:
        for t in threads:
            t.join(timeout=2)
        if proc.poll() is None:
            proc.kill()
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass
        # Record any in-flight requests that never got a response.
        now = clock()
        leftover = list(pending.items())
        pending.clear()
        for _, (req, start) in leftover:
            writer.write(spans_for_messages([req], [], start, now, agent))
