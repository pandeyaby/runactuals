"""Replay past runs from a TOML case file against real evidence.

Use this to back-test RunActuals on an agent's history before instrumenting it:
each case gives a run's start time and the status the scheduler reported, and
the configured evidence adapter checks the real repository. Read-only: the
adapter only issues GET requests.

Case file shape (see examples/why-next-replay.toml):

    agent = "lesson-builder"
    run_minutes = 25          # assumed run length when a case gives no end
    [evidence]                # any evidence adapter spec
    adapter = "github_commit_ci"
    ...
    [[runs]]
    label = "Nov 17 ~3:53 PM PT"
    start = 2026-11-17T15:53:00-08:00
    claimed = "interrupted"
    expect_head = "14cd7655"  # optional ground truth: the pushed head with CI
    source = "where the claimed status came from"
"""

from __future__ import annotations

import hashlib
import tomllib
from datetime import datetime

from . import conventions as c
from .checks import build_report
from .store import Store
from .verify import verify_runs


def _ns(t) -> int:
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    if t.tzinfo is None:
        raise ValueError(f"start time needs a UTC offset: {t}")
    return int(t.timestamp() * 1e9)


def case_spans(agent: str, case: dict, run_minutes: float) -> list[dict]:
    start = _ns(case["start"])
    end = _ns(case["end"]) if case.get("end") else start + int(run_minutes * 60e9)
    tid = hashlib.sha256(f"replay:{agent}:{case['start']}".encode()).hexdigest()[:32]
    attrs = {c.GEN_AI_OPERATION: c.OP_INVOKE_AGENT, c.GEN_AI_AGENT_NAME: agent,
             c.RA_CLAIMED_STATUS: case["claimed"], "runactuals.run.title": f"{agent}: {case['label']}",
             "runactuals.replay.source": case.get("source", "")}
    if case.get("message"):
        attrs[c.RA_STATUS_MESSAGE] = case["message"]
    return [{"trace_id": tid, "span_id": tid[:16], "parent_span_id": "", "name": f"invoke_agent {agent}",
             "start_ns": start, "end_ns": end, "status_code": 0 if case["claimed"] == "succeeded" else 2,
             "status_message": "", "attributes": attrs, "events": [],
             "resource": {"service.name": agent, "runactuals.replay": True}}]


def replay(cases_path: str, db_path: str, **adapter_kwargs) -> list[dict]:
    with open(cases_path, "rb") as f:
        cases = tomllib.load(f)
    agent, run_minutes = cases["agent"], float(cases.get("run_minutes", 25))
    store = Store(db_path)
    try:
        by_trace = {}
        for case in cases["runs"]:
            spans = case_spans(agent, case, run_minutes)
            store.add_spans(spans)
            by_trace[spans[0]["trace_id"]] = case
        verify_runs(store, {"agents": [{"name": agent, "evidence": cases["evidence"]}]},
                    recheck=True, **adapter_kwargs)
        rep = build_report(store.spans(), store.latest_evidence())
    finally:
        store.close()
    rows = []
    for r in sorted(rep.runs, key=lambda r: r.start_ns):
        case = by_trace.get(r.trace_id)
        if not case:
            continue
        o = rep.outcomes[r.trace_id]
        ev = o.evidence[0] if o.evidence else {"summary": "", "details": {}, "status": "none"}
        head = ev["details"].get("ci_sha", "")
        expect = case.get("expect_head", "")
        rows.append({
            "label": case["label"], "claimed": o.claimed, "actual": o.actual,
            "verdict": o.mismatch or "consistent", "evidence": ev["summary"],
            "lessons": len(ev["details"].get("matched", [])), "head": head[:8],
            "expect_head": expect,
            "head_ok": None if not expect else head.startswith(expect),
            "ci": [x.get("id") for x in ev["details"].get("ci", [])],
            "source": case.get("source", ""),
        })
    return rows


def print_rows(rows: list[dict]) -> None:
    print(f"{'run':22} {'claimed':11} {'verified':9} {'verdict':14} {'lessons':>7} {'head':8} {'expected':9} CI run")
    for r in rows:
        ok = "" if r["head_ok"] is None else ("ok" if r["head_ok"] else "MISMATCH")
        print(f"{r['label'][:22]:22} {r['claimed']:11} {r['actual']:9} {r['verdict']:14} {r['lessons']:7} "
              f"{r['head']:8} {(r['expect_head'] + ' ' + ok).strip():9} {','.join(map(str, r['ci']))}")
    ff = sum(r["verdict"] == "false_failure" for r in rows)
    checked = [r for r in rows if r["head_ok"] is not None]
    print(f"\nruns={len(rows)} false_failures={ff} false_successes={sum(r['verdict'] == 'false_success' for r in rows)} "
          f"unverified_or_pending={sum(r['actual'] in ('pending', 'unverified') for r in rows)} "
          f"head_matches_ground_truth={sum(r['head_ok'] for r in checked)}/{len(checked)}")
