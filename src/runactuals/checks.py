"""The four checks, as pure functions over runs and evidence.

1. outcome      did the run really succeed? (claimed vs evidence)
2. cost         what did each verified outcome cost?
3. loops/drift  is it stuck or drifting?
4. audit        what did it touch?
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import conventions as c
from .model import Run, group_runs
from .otlp import STATUS_ERROR

VERIFIED, REFUTED, PENDING, UNKNOWN = "verified", "refuted", "pending", "unknown"
FALSE_FAILURE, FALSE_SUCCESS = "false_failure", "false_success"


# ------------------------------------------------------------ 1. outcome

@dataclass
class Outcome:
    claimed: str
    actual: str  # succeeded | failed | pending | unverified
    mismatch: str | None  # false_failure | false_success | None
    evidence: list[dict] = field(default_factory=list)


def outcome(run: Run, evidence: dict[str, dict] | None) -> Outcome:
    """Compare what the run claimed with what the evidence adapters found."""
    rows = list((evidence or {}).values())
    statuses = {r["status"] for r in rows}
    if not rows:
        actual = "unverified"
    elif REFUTED in statuses:
        actual = "failed"
    elif statuses == {VERIFIED}:
        actual = "succeeded"
    else:
        actual = "pending"
    claimed = run.claimed_status
    mismatch = None
    if actual == "succeeded" and claimed != c.CLAIMED_SUCCEEDED:
        mismatch = FALSE_FAILURE
    elif actual == "failed" and claimed == c.CLAIMED_SUCCEEDED:
        mismatch = FALSE_SUCCESS
    return Outcome(claimed=claimed, actual=actual, mismatch=mismatch, evidence=rows)


# ------------------------------------------------------------ 2. cost

def run_cost(run: Run) -> float:
    """Model cost plus recorded spend (e.g. a paid eval) for one run, in USD."""
    total = 0.0
    for s in run.spans:
        a = s["attributes"]
        total += float(a.get(c.RA_COST_USD) or 0) + float(a.get(c.RA_SPEND_USD) or 0)
    return round(total, 6)


@dataclass
class CostSummary:
    total_usd: float
    runs: int
    claimed_successes: int
    verified_outcomes: int
    cost_per_claimed_success: float | None
    cost_per_verified_outcome: float | None
    unverified_usd: float  # spend on runs with no verified outcome


def cost_summary(runs: list[Run], outcomes: dict[str, Outcome]) -> CostSummary:
    total = verified_usd = 0.0
    claimed = verified = 0
    for r in runs:
        cost = run_cost(r)
        total += cost
        o = outcomes.get(r.trace_id)
        if r.claimed_status == c.CLAIMED_SUCCEEDED:
            claimed += 1
        if o and o.actual == "succeeded":
            verified += 1
            verified_usd += cost
    return CostSummary(
        total_usd=round(total, 6), runs=len(runs), claimed_successes=claimed,
        verified_outcomes=verified,
        cost_per_claimed_success=round(total / claimed, 6) if claimed else None,
        cost_per_verified_outcome=round(total / verified, 6) if verified else None,
        unverified_usd=round(total - verified_usd, 6))


# ------------------------------------------------------------ 3. loops and drift

def _tool_key(span: dict) -> tuple:
    a = span["attributes"]
    args = a.get(c.GEN_AI_TOOL_CALL_ARGS)
    if not isinstance(args, str):
        args = json.dumps(args, sort_keys=True, default=str)
    return (a.get(c.GEN_AI_TOOL_NAME) or span["name"], args)


def loop_warnings(run: Run, repeat_threshold: int = 3, retry_threshold: int = 3,
                  storm_threshold: int = 50) -> list[dict]:
    tools = sorted(run.tool_spans, key=lambda s: s["start_ns"])
    warnings = []
    best, best_key, streak, prev = 0, None, 0, None
    for s in tools:
        k = _tool_key(s)
        streak = streak + 1 if k == prev else 1
        prev = k
        if streak > best:
            best, best_key = streak, k
    if best >= repeat_threshold and best_key:
        warnings.append({"kind": "repeated_tool_call", "count": best,
                         "message": f"{best_key[0]} called {best}x in a row with the same arguments"})
    retries = 0
    for i, s in enumerate(tools):
        if s["attributes"].get(c.RA_RETRY):
            retries += 1
        elif i and tools[i - 1]["status_code"] == STATUS_ERROR and _tool_key(tools[i - 1])[0] == _tool_key(s)[0]:
            retries += 1
    if retries >= retry_threshold:
        warnings.append({"kind": "retry_storm", "count": retries,
                         "message": f"{retries} retries of failed tool calls"})
    if len(tools) >= storm_threshold:
        warnings.append({"kind": "tool_storm", "count": len(tools),
                         "message": f"{len(tools)} tool calls in one run"})
    return warnings


def run_quality(run: Run) -> float | None:
    for s in [run.root] + run.spans[::-1]:
        q = s["attributes"].get(c.RA_QUALITY)
        if q is not None:
            return float(q)
    return None


def drift_warnings(runs: list[Run], window: int = 3, min_drop: float = 0.1) -> dict[str, list[dict]]:
    """Flag the newest run of an agent when its recent quality fell versus the window before."""
    out: dict[str, list[dict]] = {}
    by_agent: dict[str, list[Run]] = {}
    for r in runs:
        if run_quality(r) is not None:
            by_agent.setdefault(r.agent, []).append(r)
    for agent, rs in by_agent.items():
        rs.sort(key=lambda r: r.start_ns)
        if len(rs) < 2 * window:
            continue
        recent = [run_quality(r) for r in rs[-window:]]
        before = [run_quality(r) for r in rs[-2 * window:-window]]
        mean_recent, mean_before = sum(recent) / window, sum(before) / window
        if mean_before - mean_recent >= min_drop:
            out.setdefault(rs[-1].trace_id, []).append({
                "kind": "quality_drift", "count": window,
                "message": (f"{agent} quality fell from {mean_before:.2f} to {mean_recent:.2f} "
                            f"over the last {window} runs")})
    return out


# ------------------------------------------------------------ 4. audit

def audit_timeline(runs: list[Run]) -> list[dict]:
    events = []
    for r in runs:
        for s in r.audits:
            a = s["attributes"]
            action = str(a.get(c.RA_AUDIT_ACTION))
            approval = a.get(c.RA_AUDIT_APPROVAL) or ""
            events.append({
                "time_ns": s["start_ns"], "trace_id": r.trace_id, "agent": r.agent,
                "action": action, "target": str(a.get(c.RA_AUDIT_TARGET) or ""),
                "actor": str(a.get(c.RA_AUDIT_ACTOR) or r.agent), "approval_ref": str(approval),
                "spend_usd": float(a[c.RA_SPEND_USD]) if a.get(c.RA_SPEND_USD) is not None else None,
                "flag": ("no approval recorded"
                         if action in c.SENSITIVE_ACTIONS and not approval else None),
            })
    events.sort(key=lambda e: e["time_ns"], reverse=True)
    return events


# ------------------------------------------------------------ report

@dataclass
class Report:
    runs: list[Run]
    outcomes: dict[str, Outcome]
    warnings: dict[str, list[dict]]
    cost: CostSummary
    audit: list[dict]

    @property
    def false_failures(self) -> list[Run]:
        return [r for r in self.runs if self.outcomes[r.trace_id].mismatch == FALSE_FAILURE]

    @property
    def false_successes(self) -> list[Run]:
        return [r for r in self.runs if self.outcomes[r.trace_id].mismatch == FALSE_SUCCESS]


def build_report(spans: list[dict], evidence: dict[str, dict]) -> Report:
    runs = group_runs(spans)
    outcomes = {r.trace_id: outcome(r, evidence.get(r.trace_id)) for r in runs}
    warnings = {r.trace_id: loop_warnings(r) for r in runs}
    for tid, ws in drift_warnings(runs).items():
        warnings.setdefault(tid, []).extend(ws)
    return Report(runs=runs, outcomes=outcomes, warnings=warnings,
                  cost=cost_summary(runs, outcomes), audit=audit_timeline(runs))
