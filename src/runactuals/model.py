"""Group raw spans into agent runs. One trace = one run."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import conventions as c
from .otlp import STATUS_ERROR


@dataclass
class Run:
    trace_id: str
    agent: str
    root: dict
    spans: list[dict] = field(default_factory=list)

    @property
    def start_ns(self) -> int:
        return self.root["start_ns"]

    @property
    def end_ns(self) -> int:
        return max([self.root["end_ns"]] + [s["end_ns"] for s in self.spans])

    @property
    def title(self) -> str:
        return str(self.root["attributes"].get("runactuals.run.title") or self.root["name"])

    @property
    def claimed_status(self) -> str:
        """What the agent (or its scheduler) said happened. Not trusted."""
        claimed = self.root["attributes"].get(c.RA_CLAIMED_STATUS)
        if claimed:
            return str(claimed)
        return c.CLAIMED_FAILED if self.root["status_code"] == STATUS_ERROR else c.CLAIMED_SUCCEEDED

    @property
    def status_message(self) -> str:
        return str(self.root["attributes"].get(c.RA_STATUS_MESSAGE) or self.root["status_message"] or "")

    def op(self, operation: str) -> list[dict]:
        return [s for s in self.spans if s["attributes"].get(c.GEN_AI_OPERATION) == operation]

    @property
    def tool_spans(self) -> list[dict]:
        return self.op(c.OP_EXECUTE_TOOL)

    @property
    def claims(self) -> list[dict]:
        return [s for s in self.spans if c.RA_CLAIM_KIND in s["attributes"]]

    @property
    def audits(self) -> list[dict]:
        return [s for s in self.spans if c.RA_AUDIT_ACTION in s["attributes"]]


def _pick_root(spans: list[dict]) -> dict:
    ids = {s["span_id"] for s in spans}
    tops = [s for s in spans if not s["parent_span_id"] or s["parent_span_id"] not in ids]
    agents = [s for s in tops if s["attributes"].get(c.GEN_AI_OPERATION) == c.OP_INVOKE_AGENT]
    return min(agents or tops or spans, key=lambda s: s["start_ns"])


def group_runs(spans: list[dict]) -> list[Run]:
    by_trace: dict[str, list[dict]] = {}
    for s in spans:
        by_trace.setdefault(s["trace_id"], []).append(s)
    runs = []
    for trace_id, group in by_trace.items():
        group.sort(key=lambda s: (s["start_ns"], s["span_id"]))
        root = _pick_root(group)
        agent = (root["attributes"].get(c.GEN_AI_AGENT_NAME)
                 or root["resource"].get("service.name") or "unknown")
        runs.append(Run(trace_id=trace_id, agent=str(agent), root=root, spans=group))
    runs.sort(key=lambda r: r.start_ns, reverse=True)
    return runs
