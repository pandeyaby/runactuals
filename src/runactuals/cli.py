"""runactuals serve | verify | demo | report | proxy | wrap"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time

from .checks import build_report, run_cost
from .server import report_json, serve
from .store import Store
from .verify import load_config, verify_runs


def _print_report(rep) -> None:
    c = rep.cost
    print(f"{'run':38} {'claimed':12} {'verified':10} {'verdict':14} {'cost':>8}  warnings")
    for r in sorted(rep.runs, key=lambda r: r.start_ns):
        o = rep.outcomes[r.trace_id]
        warns = ", ".join(w["kind"] for w in rep.warnings.get(r.trace_id, []))
        print(f"{r.title[:38]:38} {o.claimed:12} {o.actual:10} {(o.mismatch or 'consistent'):14} "
              f"${run_cost(r):7.4f}  {warns}")
    per_v = "n/a" if c.cost_per_verified_outcome is None else f"${c.cost_per_verified_outcome:.4f}"
    per_c = "n/a" if c.cost_per_claimed_success is None else f"${c.cost_per_claimed_success:.4f}"
    print(f"\nruns={c.runs} claimed_successes={c.claimed_successes} verified_outcomes={c.verified_outcomes} "
          f"false_failures={len(rep.false_failures)} false_successes={len(rep.false_successes)}")
    print(f"total=${c.total_usd:.4f} cost_per_verified_outcome={per_v} (naive per claimed success {per_c})")
    flagged = [e for e in rep.audit if e["flag"]]
    print(f"audit_events={len(rep.audit)} flagged={len(flagged)}"
          + "".join(f"\n  ! {e['action']} {e['target']}: {e['flag']}" for e in flagged))

def _explicit_db(argv: list[str] | None) -> bool:
    args = argv if argv is not None else sys.argv[1:]
    return "--db" in args or bool(os.environ.get("RUNACTUALS_DB"))


def _writer(a, argv: list[str] | None, service: str):
    from .record import SpanWriter
    db = a.db if _explicit_db(argv) else None
    endpoint = getattr(a, "endpoint", None)
    if db is None and not endpoint:
        endpoint = "http://127.0.0.1:4318"
    store = Store(db) if db else None
    return SpanWriter(store=store, endpoint=endpoint, service_name=service), store


def _upstreams(a) -> dict[str, str]:
    out = {}
    if getattr(a, "openai_upstream", None):
        out["openai"] = a.openai_upstream
    if getattr(a, "anthropic_upstream", None):
        out["anthropic"] = a.anthropic_upstream
    if getattr(a, "gemini_upstream", None):
        out["gemini"] = a.gemini_upstream
    if getattr(a, "mcp_upstream", None):
        out["mcp"] = a.mcp_upstream
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="runactuals", description="Claims versus actuals, for every agent run.")
    p.add_argument("--db", default=os.environ.get("RUNACTUALS_DB", "runactuals.db"))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="OTLP/HTTP receiver + dashboard")
    s.add_argument("--host", default=os.environ.get("RUNACTUALS_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("RUNACTUALS_PORT", "4318")))
    s.add_argument("--forward", action="append", default=[],
                   help="also send OTLP to this URL, e.g. http://datacat:4318/v1/traces (repeatable)")
    s.add_argument("--config", default=os.environ.get("RUNACTUALS_CONFIG"))
    s.add_argument("--verify-every", type=float, default=300, help="seconds between evidence checks")
    s.add_argument("--demo", action="store_true", default=os.environ.get("RUNACTUALS_DEMO") == "1",
                   help="seed synthetic demo data into an empty database first")
    s.add_argument("--proxy-port", type=int,
                   default=int(os.environ.get("RUNACTUALS_PROXY_PORT") or "0"),
                   help="also start the LLM/MCP recording proxy on this port (0 = off)")
    s.add_argument("--openai-upstream", default=os.environ.get("RUNACTUALS_OPENAI_UPSTREAM"))
    s.add_argument("--anthropic-upstream", default=os.environ.get("RUNACTUALS_ANTHROPIC_UPSTREAM"))
    s.add_argument("--gemini-upstream", default=os.environ.get("RUNACTUALS_GEMINI_UPSTREAM"))
    s.add_argument("--mcp-upstream", default=os.environ.get("RUNACTUALS_MCP_UPSTREAM"))
    s.add_argument("--proxy-agent", default=os.environ.get("RUNACTUALS_AGENT", "proxy"))

    v = sub.add_parser("verify", help="run evidence adapters once")
    v.add_argument("--config", required=True)
    v.add_argument("--recheck", action="store_true")

    sub.add_parser("demo", help="load synthetic lesson-builder runs and print the report")
    rp = sub.add_parser("replay", help="back-test past runs from a case file against real evidence")
    rp.add_argument("cases")
    r = sub.add_parser("report", help="print the four checks")
    r.add_argument("--json", action="store_true")

    px = sub.add_parser("proxy", help="HTTP proxy that records LLM and MCP calls")
    px.add_argument("--host", default=os.environ.get("RUNACTUALS_PROXY_HOST", "127.0.0.1"))
    px.add_argument("--port", type=int, default=int(os.environ.get("RUNACTUALS_PROXY_PORT") or "8787"))
    px.add_argument("--endpoint", default=os.environ.get("RUNACTUALS_ENDPOINT"),
                    help="OTLP collector, default http://127.0.0.1:4318 when --db is omitted")
    px.add_argument("--openai-upstream", default=os.environ.get("RUNACTUALS_OPENAI_UPSTREAM"))
    px.add_argument("--anthropic-upstream", default=os.environ.get("RUNACTUALS_ANTHROPIC_UPSTREAM"))
    px.add_argument("--gemini-upstream", default=os.environ.get("RUNACTUALS_GEMINI_UPSTREAM"))
    px.add_argument("--mcp-upstream", default=os.environ.get("RUNACTUALS_MCP_UPSTREAM"))
    px.add_argument("--agent", default=os.environ.get("RUNACTUALS_AGENT", "proxy"))
    px.add_argument("--timeout", type=float, default=float(os.environ.get("RUNACTUALS_PROXY_TIMEOUT") or "300"))

    w = sub.add_parser("wrap", help="stdio wrapper that records MCP JSON-RPC")
    w.add_argument("--endpoint", default=os.environ.get("RUNACTUALS_ENDPOINT"),
                   help="OTLP collector, default http://127.0.0.1:4318 when --db is omitted")
    w.add_argument("--agent", default=os.environ.get("RUNACTUALS_AGENT", "mcp"))
    w.add_argument("command", nargs=argparse.REMAINDER, help="command to wrap, after --")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if a.cmd == "demo":
        from .demo import run_demo
        _print_report(run_demo(a.db))
        print(f"\nwrote {a.db}; now run: runactuals --db {a.db} serve  and open http://127.0.0.1:4318/")
        return 0

    if a.cmd == "replay":
        from .replay import print_rows, replay
        print_rows(replay(a.cases, a.db))
        print(f"\nwrote {a.db}; view with: runactuals --db {a.db} serve")
        return 0

    if a.cmd == "serve" and a.demo:
        probe = Store(a.db)
        empty = not probe.spans()
        probe.close()
        if empty:
            from .demo import run_demo
            run_demo(a.db)
            logging.info("seeded synthetic demo data")

    if a.cmd == "proxy":
        from .proxy import serve_proxy
        writer, _owned = _writer(a, argv, "runactuals-proxy")
        srv = serve_proxy(writer, a.host, a.port, upstreams=_upstreams(a),
                          agent=a.agent, timeout=a.timeout)
        logging.info("RunActuals proxy listening on http://%s:%d", a.host, a.port)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    if a.cmd == "wrap":
        from .mcp import wrap_stdio
        command = list(a.command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            logging.error("wrap needs a command, e.g. runactuals wrap -- my-mcp-server")
            return 2
        writer, _owned = _writer(a, argv, "runactuals-wrap")
        return wrap_stdio(command, writer, agent=a.agent)

    store = Store(a.db)
    if a.cmd == "verify":
        for row in verify_runs(store, load_config(a.config), recheck=a.recheck):
            print(f"{row['status']:9} {row['agent']} {row['trace_id'][:8]} {row['summary']}")
        return 0
    if a.cmd == "report":
        if a.json:
            print(json.dumps(report_json(store), indent=1))
        else:
            _print_report(build_report(store.spans(), store.latest_evidence()))
        return 0

    srv = serve(store, a.host, a.port, forward=a.forward,
                ingest_token=os.environ.get("RUNACTUALS_INGEST_TOKEN") or None)
    if getattr(a, "proxy_port", 0):
        from .proxy import serve_proxy
        from .record import SpanWriter
        proxy = serve_proxy(SpanWriter(store=store, service_name="runactuals-proxy"),
                            a.host, a.proxy_port, upstreams=_upstreams(a),
                            agent=getattr(a, "proxy_agent", "proxy"))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        logging.info("LLM/MCP proxy listening on http://%s:%d", a.host, a.proxy_port)
    if a.config:
        cfg = load_config(a.config)

        def loop():
            while True:
                try:
                    verify_runs(store, cfg)
                except Exception:  # keep serving even if an adapter breaks
                    logging.exception("verify failed")
                time.sleep(a.verify_every)
        threading.Thread(target=loop, daemon=True).start()
    logging.info("RunActuals listening on http://%s:%d (OTLP at /v1/traces)", a.host, a.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
