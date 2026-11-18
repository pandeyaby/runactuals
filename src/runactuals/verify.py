"""Run the configured evidence adapters over stored runs."""

from __future__ import annotations

import tomllib

from .adapters import build_adapter
from .model import group_runs
from .store import Store

FINAL = {"verified", "refuted"}


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def verify_runs(store: Store, config: dict, recheck: bool = False, **adapter_kwargs) -> list[dict]:
    """Check every run whose agent has evidence configured. Returns what was recorded."""
    agents = {a["name"]: a for a in config.get("agents", [])}
    latest = store.latest_evidence()
    adapters: dict[str, list] = {}
    recorded = []
    for run in group_runs(store.spans()):
        spec = agents.get(run.agent)
        if not spec:
            continue
        if run.agent not in adapters:
            specs = spec.get("evidence", [])
            specs = [specs] if isinstance(specs, dict) else specs
            adapters[run.agent] = [build_adapter(s, **adapter_kwargs) for s in specs]
        for adapter in adapters[run.agent]:
            prev = latest.get(run.trace_id, {}).get(adapter.name)
            if prev and prev["status"] in FINAL and not recheck:
                continue
            ev = adapter.check(run)
            store.add_evidence(run.trace_id, ev.adapter, ev.status, ev.summary, ev.url, ev.details)
            recorded.append({"trace_id": run.trace_id, "agent": run.agent, "status": ev.status,
                             "summary": ev.summary})
    return recorded
