"""Replay of the real Nov 17 3:53 PM PT why-next run, offline, against recorded API answers."""

import os
import tempfile
import tomllib
import unittest

from runactuals.replay import replay

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# visual-draft, Nov 18 00:01-00:30 UTC, newest first (as GitHub returned them)
WINDOW = [
    ("14cd7655", "Queue: age-mix note for the Nov 17 ~3:53 PM run; wave 8 topics lead the queue"),
    ("2ac9ea4f", "Guide: embed vectors for 2 new lessons (976/976)"),
    ("b5ed1513", "Pipeline: flagship lesson First, second, third (ordinal numbers) (mt_0PjZYkLO2e)"),
    ("6962f71e", "Pipeline: flagship lesson Same first sound (alliteration) (mt_t85_xApNiM)"),
    ("1160438f", "Wave 8: 15 new young topics in the viewer graph"),
]
CI = {"14cd7655": [{"id": 44208356938, "name": "validate", "workflow_id": 1, "status": "completed",
                    "conclusion": "success", "html_url": "https://github.com/pandeyaby/why-next/actions/runs/44208356938"}]}

CASES = """
agent = "lesson-builder"
run_minutes = 25
[evidence]
adapter = "github_commit_ci"
repo = "pandeyaby/why-next"
branch = "visual-draft"
message_regex = "^Pipeline: flagship lesson "
workflows = ["validate"]
grace_minutes = 90
auth = "gh"
[[runs]]
label = "Nov 17 ~3:53 PM PT"
start = 2026-11-17T15:53:00-08:00
claimed = "interrupted"
expect_head = "14cd7655"
"""


def fetch(path):
    if "/commits?" in path:
        return [{"sha": s, "commit": {"message": m}} for s, m in WINDOW]
    if "/actions/runs?" in path:
        return {"workflow_runs": CI.get(path.split("head_sha=")[1].split("&")[0], [])}
    raise AssertionError(path)


class ReplayTest(unittest.TestCase):
    def test_interrupted_run_is_a_false_failure_with_the_real_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = os.path.join(tmp, "cases.toml")
            with open(cases, "w") as f:
                f.write(CASES)
            [row] = replay(cases, os.path.join(tmp, "r.db"), fetch=fetch)
        self.assertEqual((row["claimed"], row["actual"], row["verdict"]),
                         ("interrupted", "succeeded", "false_failure"))
        self.assertEqual((row["lessons"], row["head"], row["head_ok"]), (2, "14cd7655", True))
        self.assertEqual(row["ci"], [44208356938])

    def test_example_case_file_is_valid(self):
        with open(os.path.join(ROOT, "examples", "why-next-replay.toml"), "rb") as f:
            cases = tomllib.load(f)
        self.assertEqual(cases["evidence"]["adapter"], "github_commit_ci")
        self.assertTrue(all(r["start"].tzinfo for r in cases["runs"]))
        self.assertEqual(len(cases["runs"]), 18)


if __name__ == "__main__":
    unittest.main()
