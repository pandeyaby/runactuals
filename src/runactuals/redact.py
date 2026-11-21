"""Redact secrets from headers, URLs, and payloads before they are stored."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

REDACTED = "[REDACTED]"

SECRET_HEADER_NAMES = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "api-key", "x-goog-api-key", "x-auth-token", "x-access-token",
    "x-amz-security-token", "openai-api-key", "anthropic-api-key",
})

SECRET_QUERY_KEYS = frozenset({
    "key", "api_key", "apikey", "access_token", "token", "password", "secret",
    "client_secret", "refresh_token",
})

SECRET_JSON_KEYS = frozenset({
    "api_key", "apikey", "access_token", "accesstoken", "refresh_token",
    "refreshtoken", "password", "secret", "authorization", "client_secret",
    "clientsecret", "private_key", "privatekey", "id_token", "session_token",
})

# Values that look like tokens even when the field name is innocent.
_SECRET_VALUE = re.compile(
    r"(?i)(?P<full>"
    r"Bearer\s+\S+"
    r"|sk-[A-Za-z0-9_-]{8,}"
    r"|sk-ant-[A-Za-z0-9_-]{8,}"
    r"|sk-proj-[A-Za-z0-9_-]{8,}"
    r"|AIza[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[A-Z0-9]{16}"
    r")"
)


def redact_value(value: str) -> str:
    return _SECRET_VALUE.sub(REDACTED, value)


def _is_secret_json_key(key: str) -> bool:
    compact = key.lower().replace("-", "_")
    if compact in SECRET_JSON_KEYS or compact in {"api_key", "token"}:
        return True
    if compact == "key":
        return False  # decided from the value; "key" is too common
    return "api_key" in compact or compact.endswith("_secret") or compact.endswith("_token") and compact != "encoding_token"


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _is_secret_json_key(k):
                out[k] = REDACTED
            elif isinstance(k, str) and k.lower() == "key" and isinstance(v, str) and _SECRET_VALUE.search(v):
                out[k] = REDACTED
            else:
                out[k] = _redact_json(v)
        return out
    if isinstance(value, list):
        return [_redact_json(v) for v in value]
    if isinstance(value, str):
        return redact_value(value)
    return value


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (headers or {}).items():
        if k.lower() in SECRET_HEADER_NAMES:
            out[k] = REDACTED
        else:
            out[k] = redact_value(str(v))
    return out


def redact_url(url: str) -> str:
    p = urlparse(url)
    parts = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        if k.lower() in SECRET_QUERY_KEYS or _SECRET_VALUE.search(v):
            parts.append(f"{quote_plus(k)}={REDACTED}")
        else:
            parts.append(urlencode([(k, v)]))
    return urlunparse((p.scheme, p.netloc, p.path, p.params, "&".join(parts), p.fragment))


def redact_path_and_query(path: str) -> str:
    """Redact query parameters on a request path (no host)."""
    if "://" in path:
        return redact_url(path)
    return redact_url("http://local" + (path if path.startswith("/") else "/" + path))[len("http://local"):]


def redact_body(body: bytes | str | None, limit: int = 8192) -> str:
    """Return a redacted, truncated unicode preview of a request or response body."""
    if body is None:
        return ""
    if isinstance(body, bytes):
        text = body.decode("utf-8", "replace")
    else:
        text = body
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            text = json.dumps(_redact_json(json.loads(stripped)), ensure_ascii=False)
        except ValueError:
            text = redact_value(text)
    else:
        text = redact_value(text)
    if len(text) > limit:
        return text[:limit] + f"…[{len(text) - limit} more chars]"
    return text
