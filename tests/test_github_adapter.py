import unittest
import urllib.error

from runactuals import conventions as c
from runactuals.adapters import build_adapter
from runactuals.model import group_runs

from helpers import root, span

S = 1_000_000_000
START, END = 1_800_000_000 * S, (1_800_000_000 + 1260) * S  # a 21-minute run


class FakeGitHub:
    def __init__(self, commits=(), ci=None, on_branch=()):
        self.commits, self.ci, self.on_branch, self.calls = list(commits), ci or {}, set(on_branch), []

    def __call__(self, path):
        self.calls.append(path)
        if "/commits?" in path:
            return self.commits
        if "/compare/" in path:
            return {"status": "behind" if path.rsplit("...", 1)[1] in self.on_branch else "diverged"}
        if "/actions/runs?" in path:
            sha = path.split("head_sha=")[1].split("&")[0]
            return {"workflow_runs": self.ci.get(sha, [])}
        raise AssertionError(path)


def commit(sha, msg="lesson: Telescopes"):
    return {"sha": sha, "commit": {"message": msg}}


def ci(conclusion="success", status="completed", name="CI", id=44166327238):
    return {"id": id, "name": name, "status": status, "conclusion": conclusion, "html_url": f"u/{id}"}


def adapter(fake, now_s=1_800_000_000 + 4 * 3600, **kw):
    spec = {"adapter": "github_commit_ci", "repo": "o/r", "branch": "visual-draft",
            "message_regex": "^lesson: ", "grace_minutes": 45, **kw}
    return build_adapter(spec, fetch=fake, now=lambda: now_s)


def run(claimed="interrupted", sha=None):
    spans = [root(claimed=claimed, start=START, end=END)]
    if sha:
        spans.append(span(start=START + 5, **{c.RA_CLAIM_KIND: "git_commit",
                                              c.VCS_REF_HEAD_NAME: "visual-draft",
                                              c.VCS_REF_HEAD_REVISION: sha}))
    return group_runs(spans)[0]


class GitHubCommitCITest(unittest.TestCase):
    def test_interrupted_run_that_pushed_with_green_ci_is_verified(self):
        fake = FakeGitHub([commit("abc123")], {"abc123": [ci()]})
        e = adapter(fake).check(run("interrupted"))
        self.assertEqual(e.status, "verified")
        self.assertEqual(e.details["sha"], "abc123")
        self.assertIn("44166327238", e.summary)
        self.assertIn("since=", fake.calls[0])

    def test_message_regex_skips_unrelated_commits(self):
        fake = FakeGitHub([commit("x1", "chore: bump"), commit("abc", "lesson: K-Pg")],
                          {"abc": [ci()]})
        self.assertEqual(adapter(fake).check(run()).details["sha"], "abc")

    def test_claimed_sha_must_be_on_branch(self):
        fake = FakeGitHub(ci={"dead": [ci()]})
        e = adapter(fake).check(run("succeeded", sha="dead"))
        self.assertEqual(e.status, "refuted")
        self.assertIn("not on visual-draft", e.summary)

    def test_claimed_sha_with_failed_ci_is_refuted(self):
        fake = FakeGitHub(ci={"d1": [ci("failure")]}, on_branch={"d1"})
        self.assertEqual(adapter(fake).check(run("succeeded", sha="d1")).status, "refuted")

    def test_ci_running_is_pending(self):
        fake = FakeGitHub([commit("a")], {"a": [ci(None, "in_progress")]})
        self.assertEqual(adapter(fake).check(run()).status, "pending")

    def test_workflow_filter(self):
        fake = FakeGitHub([commit("a")], {"a": [ci(), ci("failure", name="Nightly", id=2)]})
        self.assertEqual(adapter(fake, workflows=["CI"]).check(run()).status, "verified")
        self.assertEqual(adapter(fake).check(run()).status, "refuted")

    def test_no_commit_is_pending_inside_grace_then_refuted(self):
        fake = FakeGitHub()
        inside = adapter(fake, now_s=1_800_000_000 + 1260 + 600).check(run())
        after = adapter(fake).check(run())
        self.assertEqual((inside.status, after.status), ("pending", "refuted"))

    def test_network_error_is_unknown_not_a_crash(self):
        def boom(path):
            raise urllib.error.URLError("offline")
        self.assertEqual(adapter(boom).check(run()).status, "unknown")

    def test_unknown_adapter_name(self):
        with self.assertRaises(ValueError):
            build_adapter({"adapter": "nope"})


if __name__ == "__main__":
    unittest.main()
