"""SDK -> OTLP/HTTP -> recorder -> evidence adapter -> checks -> dashboard."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from runactuals.demo import DEMO_CONFIG, run_demo
from runactuals.sdk import Recorder
from runactuals.server import serve
from runactuals.store import Store
from runactuals.verify import load_config

from helpers import pb_request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Server:
    def __init__(self, db, **kw):
        self.store = Store(db)
        self.srv = serve(self.store, port=0, **kw)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def get(self, path):
        with urllib.request.urlopen(self.url + path) as r:
            return r.read().decode()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.store.close()


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "ra.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_demo_shows_the_lesson_builder_false_failures(self):
        rep = run_demo(self.db)
        self.assertEqual(len(rep.runs), 6)
        self.assertEqual(sorted(r.title for r in rep.false_failures),
                         ["Build lesson: K-Pg Extinction", "Build lesson: Mixed Numbers"])
        self.assertEqual([r.title for r in rep.false_successes], ["Build lesson: Telescopes"])
        self.assertEqual((rep.cost.claimed_successes, rep.cost.verified_outcomes), (3, 4))
        kinds = {w["kind"] for ws in rep.warnings.values() for w in ws}
        self.assertTrue({"repeated_tool_call", "retry_storm", "quality_drift"} <= kinds)
        self.assertEqual(sum(1 for e in rep.audit if e["flag"]), 1)

        srv = Server(self.db)
        try:
            html = srv.get("/")
            self.assertIn("FALSE FAILURE", html)
            self.assertIn("timed out after 21 min waiting for learn-e2e", html)
            self.assertIn("no approval recorded", html)
            api = json.loads(srv.get("/api/report"))
            self.assertEqual(api["summary"]["false_failures"], 2)
            tid = rep.false_failures[0].trace_id
            self.assertIn("Evidence", srv.get(f"/runs/{tid}"))
        finally:
            srv.close()

    def test_example_config_matches_demo(self):
        self.assertEqual(load_config(os.path.join(ROOT, "examples", "lesson-builder.toml")), DEMO_CONFIG)

    def test_sdk_exception_marks_run_failed(self):
        srv = Server(self.db)
        try:
            rec = Recorder(srv.url, service_name="bot")
            with self.assertRaises(RuntimeError):
                with rec.run("bot") as run:
                    run.tool("x", {"a": 1})
                    raise RuntimeError("boom")
            api = json.loads(srv.get("/api/report"))
            [r] = api["runs"]
            self.assertEqual((r["agent"], r["claimed"], r["actual"]), ("bot", "failed", "unverified"))
        finally:
            srv.close()

    def test_protobuf_ingest_and_ingest_token(self):
        srv = Server(self.db, ingest_token="s3cret")
        try:
            body = pb_request(b"\x09" * 16, b"\x08" * 8, "invoke_agent pb", 1, 2,
                              {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "pb"})
            req = urllib.request.Request(srv.url + "/v1/traces", data=body, method="POST",
                                         headers={"Content-Type": "application/x-protobuf"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 401)
            req.add_header("Authorization", "Bearer s3cret")
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(srv.get("/api/report"))["runs"][0]["agent"], "pb")
        finally:
            srv.close()

    def test_fan_out_forwards_raw_otlp(self):
        sink = Server(os.path.join(self.tmp.name, "sink.db"))
        srv = Server(self.db, forward=[sink.url + "/v1/traces"])
        try:
            Recorder(srv.url).run("fwd").end()
            for _ in range(50):
                if sink.store.spans():
                    break
                threading.Event().wait(0.05)
            self.assertEqual(json.loads(sink.get("/api/report"))["runs"][0]["agent"], "fwd")
        finally:
            srv.close()
            sink.close()


if __name__ == "__main__":
    unittest.main()
