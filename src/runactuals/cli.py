"""runactuals serve | verify | demo | report"""

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

    v = sub.add_parser("verify", help="run evidence adapters once")
    v.add_argument("--config", required=True)
    v.add_argument("--recheck", action="store_true")

    sub.add_parser("demo", help="load synthetic lesson-builder runs and print the report")
    r = sub.add_parser("report", help="print the four checks")
    r.add_argument("--json", action="store_true")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if a.cmd == "demo":
        from .demo import run_demo
        _print_report(run_demo(a.db))
        print(f"\nwrote {a.db}; now run: runactuals --db {a.db} serve  and open http://127.0.0.1:4318/")
        return 0

    if a.cmd == "serve" and a.demo:
        probe = Store(a.db)
        empty = not probe.spans()
        probe.close()
        if empty:
            from .demo import run_demo
            run_demo(a.db)
            logging.info("seeded synthetic demo data")

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
