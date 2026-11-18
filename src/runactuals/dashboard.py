"""Server-rendered HTML dashboard. No JavaScript, no build step."""

from __future__ import annotations

from datetime import datetime
from html import escape

from .checks import FALSE_FAILURE, FALSE_SUCCESS, Report, run_cost

CSS = """
:root{--fg:#111827;--muted:#4b5563;--bg:#ffffff;--line:#d1d5db;--card:#f9fafb}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--fg);background:var(--bg)}
header{padding:20px 28px;border-bottom:1px solid var(--line)}h1{margin:0;font-size:22px}
.tag{color:var(--muted);margin:2px 0 0}main{padding:20px 28px;max-width:1300px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card b{display:block;font-size:24px}.card span{color:var(--muted);font-size:13px}
table{border-collapse:collapse;width:100%;margin-bottom:28px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.b{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.ok{background:#dcfce7;color:#14532d}.bad{background:#fee2e2;color:#7f1d1d}.warn{background:#fef3c7;color:#78350f}.na{background:#e5e7eb;color:#1f2937}
.ff{background:#1e3a8a;color:#ffffff}.fs{background:#7f1d1d;color:#ffffff}
small{color:var(--muted)}a{color:#1d4ed8}h2{font-size:17px;margin:8px 0 10px}code{font-size:13px}
"""

_STATUS_CLS = {"succeeded": "ok", "verified": "ok", "failed": "bad", "refuted": "bad",
               "interrupted": "bad", "pending": "warn", "unknown": "na", "unverified": "na"}


def _badge(text: str, cls: str | None = None) -> str:
    return f'<span class="b {cls or _STATUS_CLS.get(text, "na")}">{escape(text)}</span>'


def _t(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9).astimezone().strftime("%b %d %H:%M %Z")


def _usd(v: float | None) -> str:
    return "n/a" if v is None else f"${v:.4f}" if v < 1 else f"${v:,.2f}"


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{escape(title)}</title><style>{CSS}</style><header><h1>RunActuals</h1>"
            f"<p class=tag>Claims versus actuals, for every agent run.</p></header><main>{body}</main></html>")


def render_index(report: Report) -> str:
    c = report.cost
    loops = sum(1 for ws in report.warnings.values() if ws)
    flagged = sum(1 for e in report.audit if e["flag"])
    cards = [
        (len(report.runs), "runs recorded"),
        (f"{c.claimed_successes} / {c.verified_outcomes}", "claimed successes / verified outcomes"),
        (len(report.false_failures), "marked failed, actually shipped"),
        (len(report.false_successes), "marked succeeded, evidence says no"),
        (_usd(c.cost_per_verified_outcome), f"cost per verified outcome (vs {_usd(c.cost_per_claimed_success)} per claimed)"),
        (loops, "runs with loop or drift warnings"),
        (flagged, "sensitive actions with no approval"),
    ]
    html = ['<div class=cards>'] + [f"<div class=card><b>{escape(str(v))}</b><span>{escape(k)}</span></div>"
                                    for v, k in cards] + ["</div>"]
    html.append("<h2>Runs: claimed vs verified</h2><table><tr><th>Started</th><th>Agent / run</th>"
                "<th>Claimed</th><th>Verified</th><th>Evidence</th><th>Cost</th><th>Warnings</th></tr>")
    for r in report.runs:
        o = report.outcomes[r.trace_id]
        verdict = _badge(o.actual)
        if o.mismatch == FALSE_FAILURE:
            verdict += " " + _badge("FALSE FAILURE", "ff")
        elif o.mismatch == FALSE_SUCCESS:
            verdict += " " + _badge("FALSE SUCCESS", "fs")
        ev = "<br>".join(
            (f'<a href="{escape(e["url"])}">{escape(e["summary"])}</a>' if e["url"] else escape(e["summary"]))
            for e in o.evidence) or "<small>no adapter configured</small>"
        warns = "<br>".join(_badge(w["kind"], "warn") + f" <small>{escape(w['message'])}</small>"
                            for w in report.warnings.get(r.trace_id, [])) or "<small>none</small>"
        msg = f"<br><small>{escape(r.status_message)}</small>" if r.status_message else ""
        html.append(
            f"<tr><td>{_t(r.start_ns)}</td><td><a href='/runs/{escape(r.trace_id)}'>{escape(r.title)}</a>"
            f"<br><small>{escape(r.agent)}</small></td><td>{_badge(r.claimed_status)}{msg}</td>"
            f"<td>{verdict}</td><td>{ev}</td><td>{_usd(run_cost(r))}</td><td>{warns}</td></tr>")
    html.append("</table>")
    html.append(render_audit(report.audit))
    return _page("RunActuals", "".join(html))


def render_audit(events: list[dict]) -> str:
    html = ["<h2>Audit timeline: what agents touched</h2><table><tr><th>When</th><th>Agent</th>"
            "<th>Action</th><th>Target</th><th>Approval</th><th>Spend</th></tr>"]
    for e in events:
        approval = escape(e["approval_ref"]) if e["approval_ref"] else ""
        if e["flag"]:
            approval = _badge(e["flag"], "bad")
        html.append(f"<tr><td>{_t(e['time_ns'])}</td><td>{escape(e['agent'])}</td>"
                    f"<td>{_badge(e['action'], 'na')}</td><td><code>{escape(e['target'])}</code></td>"
                    f"<td>{approval}</td><td>{'' if e['spend_usd'] is None else _usd(e['spend_usd'])}</td></tr>")
    if not events:
        html.append("<tr><td colspan=6><small>No audit events yet.</small></td></tr>")
    html.append("</table>")
    return "".join(html)


def render_run(report: Report, trace_id: str) -> str | None:
    run = next((r for r in report.runs if r.trace_id == trace_id), None)
    if not run:
        return None
    o = report.outcomes[trace_id]
    html = [f"<p><a href='/'>&larr; all runs</a></p><h2>{escape(run.title)}</h2>",
            f"<p>Agent <b>{escape(run.agent)}</b> &middot; trace <code>{escape(trace_id)}</code><br>"
            f"Claimed {_badge(run.claimed_status)} &middot; verified {_badge(o.actual)} "
            f"&middot; cost {_usd(run_cost(run))}</p>"]
    html.append("<h2>Evidence</h2><table><tr><th>Adapter</th><th>Status</th><th>Summary</th></tr>")
    for e in o.evidence:
        html.append(f"<tr><td>{escape(e['adapter'])}</td><td>{_badge(e['status'])}</td>"
                    f"<td>{escape(e['summary'])}</td></tr>")
    html.append("</table><h2>Spans</h2><table><tr><th>Offset</th><th>Name</th><th>Status</th><th>Attributes</th></tr>")
    for s in run.spans:
        attrs = ", ".join(f"{escape(k)}={escape(str(v))}" for k, v in s["attributes"].items())
        st = {0: "unset", 1: "ok", 2: "error"}.get(s["status_code"], "?")
        html.append(f"<tr><td>+{(s['start_ns'] - run.start_ns) / 1e9:.0f}s</td><td>{escape(s['name'])}</td>"
                    f"<td>{_badge(st, 'bad' if st == 'error' else 'na')}</td><td><small>{attrs}</small></td></tr>")
    html.append("</table>")
    return _page(run.title, "".join(html))
