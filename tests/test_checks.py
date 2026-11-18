import unittest

from runactuals import conventions as c
from runactuals.checks import (FALSE_FAILURE, FALSE_SUCCESS, audit_timeline, cost_summary,
                               drift_warnings, loop_warnings, outcome)
from runactuals.model import group_runs

from helpers import root, span, tool


def ev(status):
    return {"a": {"adapter": "a", "status": status, "summary": "", "url": "", "details": {}}}


class OutcomeTest(unittest.TestCase):
    def test_marked_failed_but_verified_is_false_failure(self):
        [run] = group_runs([root(claimed="interrupted")])
        o = outcome(run, ev("verified"))
        self.assertEqual((o.actual, o.mismatch), ("succeeded", FALSE_FAILURE))

    def test_claimed_success_refuted_is_false_success(self):
        [run] = group_runs([root(claimed="succeeded")])
        self.assertEqual(outcome(run, ev("refuted")).mismatch, FALSE_SUCCESS)

    def test_no_evidence_is_unverified_not_trusted(self):
        [run] = group_runs([root(claimed="succeeded")])
        o = outcome(run, None)
        self.assertEqual((o.actual, o.mismatch), ("unverified", None))

    def test_pending_evidence(self):
        [run] = group_runs([root(claimed="failed")])
        self.assertEqual(outcome(run, ev("pending")).actual, "pending")

    def test_claimed_status_falls_back_to_otel_status(self):
        r = root()
        del r["attributes"][c.RA_CLAIMED_STATUS]
        r["status_code"] = 2
        [run] = group_runs([r])
        self.assertEqual(run.claimed_status, "failed")


class CostTest(unittest.TestCase):
    def test_cost_per_verified_outcome(self):
        spans = []
        for t, claimed in (("a", "succeeded"), ("b", "failed"), ("c", "succeeded")):
            spans += [root(t, claimed=claimed), span(t, **{c.RA_COST_USD: 0.02}),
                      span(t, **{c.RA_SPEND_USD: 0.01})]
        runs = group_runs(spans)
        outs = {r.trace_id: outcome(r, ev({"a": "verified", "b": "verified", "c": "refuted"}[r.trace_id]))
                for r in runs}
        s = cost_summary(runs, outs)
        self.assertAlmostEqual(s.total_usd, 0.09)
        self.assertEqual((s.claimed_successes, s.verified_outcomes), (2, 2))
        self.assertAlmostEqual(s.cost_per_verified_outcome, 0.045)
        self.assertAlmostEqual(s.unverified_usd, 0.03)

    def test_no_verified_outcomes(self):
        runs = group_runs([root()])
        self.assertIsNone(cost_summary(runs, {}).cost_per_verified_outcome)


class LoopTest(unittest.TestCase):
    def test_repeated_identical_tool_calls(self):
        spans = [root()] + [tool(name="wait_for_ci", args='{"job":"e2e"}', start=i) for i in range(1, 6)]
        [w] = loop_warnings(group_runs(spans)[0])
        self.assertEqual((w["kind"], w["count"]), ("repeated_tool_call", 5))

    def test_different_args_are_not_a_loop(self):
        spans = [root()] + [tool(name="read", args=f'{{"f":{i}}}', start=i) for i in range(1, 6)]
        self.assertEqual(loop_warnings(group_runs(spans)[0]), [])

    def test_retry_storm_and_tool_storm(self):
        spans = [root()] + [tool(name="render", args=str(i), start=i, status=2) for i in range(1, 5)]
        spans += [tool(name=f"t{i}", start=10 + i) for i in range(50)]
        kinds = {w["kind"] for w in loop_warnings(group_runs(spans)[0])}
        self.assertEqual(kinds, {"retry_storm", "tool_storm"})

    def test_quality_drift(self):
        scores = [1.0, 1.0, 0.95, 0.8, 0.78, 0.74]
        spans = [root(f"r{i}", start=i * 10, **{c.RA_QUALITY: q}) for i, q in enumerate(scores)]
        out = drift_warnings(group_runs(spans))
        self.assertEqual(list(out), ["r5"])
        self.assertEqual(out["r5"][0]["kind"], "quality_drift")

    def test_stable_quality_no_drift(self):
        spans = [root(f"r{i}", start=i, **{c.RA_QUALITY: 0.9}) for i in range(6)]
        self.assertEqual(drift_warnings(group_runs(spans)), {})


class AuditTest(unittest.TestCase):
    def test_sensitive_action_without_approval_is_flagged(self):
        spans = [root(),
                 span(start=2, **{c.RA_AUDIT_ACTION: "send", c.RA_AUDIT_TARGET: "email:x"}),
                 span(start=1, **{c.RA_AUDIT_ACTION: "spend", c.RA_AUDIT_TARGET: "eval",
                                  c.RA_AUDIT_APPROVAL: "rule 7", c.RA_SPEND_USD: 0.02}),
                 span(start=3, **{c.RA_AUDIT_ACTION: "write", c.RA_AUDIT_TARGET: "repo"})]
        events = audit_timeline(group_runs(spans))
        self.assertEqual([e["action"] for e in events], ["write", "send", "spend"])
        self.assertEqual([e["flag"] for e in events], [None, "no approval recorded", None])
        self.assertEqual(events[2]["spend_usd"], 0.02)


if __name__ == "__main__":
    unittest.main()
