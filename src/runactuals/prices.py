"""USD cost from a model price table.

Prices are list rates in USD per 1 million tokens, last reviewed 2026-11-20.
They will go stale; override with a JSON file at ``RUNACTUALS_PRICES`` or
pass a mapping to ``cost_usd``. Unknown models leave cost unset.
"""

from __future__ import annotations

import json
import os
from typing import Mapping

# input / output rates, USD per 1M tokens. Embeddings use input only.
# Keys are lowercase prefixes; the longest matching prefix wins.
PRICES: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-5-nano": {"input": 0.05, "output": 0.40},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    "gpt-5": {"input": 1.25, "output": 10.00},
    "o1-mini": {"input": 1.10, "output": 4.40},
    "o1": {"input": 15.00, "output": 60.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
    "o3": {"input": 2.00, "output": 8.00},
    "o4-mini": {"input": 1.10, "output": 4.40},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "text-embedding-3-large": {"input": 0.13, "output": 0.0},
    "text-embedding-ada-002": {"input": 0.10, "output": 0.0},
    # Anthropic
    "claude-haiku-3.5": {"input": 0.80, "output": 4.00},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.00},
    "claude-3-haiku": {"input": 0.25, "output": 1.25},
    "claude-3-opus": {"input": 15.00, "output": 75.00},
    "claude-opus-4": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-7-sonnet": {"input": 3.00, "output": 15.00},
    "claude-haiku-4": {"input": 1.00, "output": 5.00},
    "claude-opus-4.5": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4.5": {"input": 3.00, "output": 15.00},
    # Google Gemini (AI Studio / Gemini API)
    "gemini-2.0-flash-lite": {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "gemini-3-flash": {"input": 0.50, "output": 3.00},
    "gemini-3-pro": {"input": 2.00, "output": 12.00},
    "gemini-embedding": {"input": 0.15, "output": 0.0},
    "text-embedding-004": {"input": 0.00, "output": 0.0},
    "text-embedding-005": {"input": 0.00, "output": 0.0},
}


def normalize_model(model: str | None) -> str:
    if not model:
        return ""
    name = model.strip().lower()
    if "/" in name:
        # "models/gemini-2.0-flash" or "publishers/google/models/gemini-2.0-flash"
        name = name.rsplit("/", 1)[-1]
    return name


def _boundary_ok(model: str, key: str) -> bool:
    if not model.startswith(key):
        return False
    if len(model) == len(key):
        return True
    return model[len(key)] in "-._@"


def lookup(model: str | None, table: Mapping[str, Mapping[str, float]] | None = None) -> dict[str, float] | None:
    """Return ``{"input", "output"}`` rates for ``model``, or None if unknown."""
    name = normalize_model(model)
    if not name:
        return None
    prices = table if table is not None else load_prices()
    hits = [k for k in prices if _boundary_ok(name, k.lower())]
    if not hits:
        return None
    key = max(hits, key=len)
    row = prices[key] if key in prices else prices[key.lower()]
    return {"input": float(row.get("input") or 0), "output": float(row.get("output") or 0)}


def cost_usd(model: str | None, input_tokens: int = 0, output_tokens: int = 0,
             table: Mapping[str, Mapping[str, float]] | None = None) -> float | None:
    rates = lookup(model, table)
    if rates is None:
        return None
    usd = (input_tokens / 1_000_000) * rates["input"] + (output_tokens / 1_000_000) * rates["output"]
    return round(usd, 8)


def load_prices(path: str | None = None) -> dict[str, dict[str, float]]:
    """Built-in table, overlaid by a JSON object of ``{model: {input, output}}``."""
    out = {k: dict(v) for k, v in PRICES.items()}
    path = path or os.environ.get("RUNACTUALS_PRICES")
    if not path:
        return out
    with open(path, encoding="utf-8") as f:
        overlay = json.load(f)
    if not isinstance(overlay, dict):
        raise ValueError("price file must be a JSON object of model -> {input, output}")
    for k, v in overlay.items():
        if not isinstance(v, dict):
            continue
        out[str(k).lower()] = {"input": float(v.get("input") or 0), "output": float(v.get("output") or 0)}
    return out
