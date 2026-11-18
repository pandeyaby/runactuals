"""Synthetic demo: six lesson-builder runs, modelled on the Why Next? false-failure case.

Everything goes through the real path: SDK -> OTLP/HTTP -> recorder -> SQLite ->
GitHub evidence adapter (against a fixture GitHub API, so no network or token)
-> the four checks. SHAs and CI run ids are synthetic.
"""

from __future__ import annotations

import hashlib
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from .checks import Report, build_report
from .sdk import Recorder
from .server import serve
from .store import Store
from .verify import verify_runs

REPO, BRANCH = "pandeyaby/why-next", "visual-draft"

DEMO_CONFIG = {"agents": [{"name": "lesson-builder", "evidence": {
    "adapter": "github_commit_ci", "repo": REPO, "branch": BRANCH,
    "message_regex": "^lesson: ", "workflows": ["CI"], "grace_minutes": 45,
    "token_env": "GITHUB_TOKEN"}}]}

# title, claimed status, status message, minutes-to-commit (None = no commit),
# report SHA in a claim?, CI conclusion, quality, extras
RUNS = [
    ("Thunder and Lightning", "succeeded", None, 13, True, "success", 1.00, {}),
    ("K-Pg Extinction", "interrupted", "timed out after 21 min waiting for learn-e2e", 14, False,
     "success", 1.00, {"wait_loop": 12}),
    ("Mixed Numbers", "failed", "killed by scheduler (exit 137)", 12, False, "success", 0.96, {}),
    ("Telescopes", "succeeded", None, 11, True, "failure", 0.81, {}),
    ("Tectonic Plates", "failed", "image step failed 4 times", None, False, None, 0.78,
     {"retries": 4, "unapproved_spend": 0.01}),
    ("Area of Compound Shapes", "succeeded", None, 12, True, "success", 0.74, {}),
]


def _sha(title: str) -> str:
    return hashlib.sha1(f"runactuals-demo:{title}".encode()).hexdigest()


class FakeClock:
    def __init__(self, start_s: float):
        self.ns = int(start_s * 1e9)

    def __call__(self) -> int:
        return self.ns

    def advance(self, seconds: float) -> None:
        self.ns += int(seconds * 1e9)

    def set(self, seconds: float) -> None:
        self.ns = int(seconds * 1e9)


class FixtureGitHub:
    """Answers the three GitHub REST calls the adapter makes, from synthetic data."""

    def __init__(self):
        self.commits: list[dict] = []  # newest first after sort
        self.ci: dict[str, list[dict]] = {}

    def add(self, title: str, at_s: float, conclusion: str, run_id: int) -> str:
        sha = _sha(title)
        date = datetime.fromtimestamp(at_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.commits.append({"sha": sha, "commit": {"message": f"lesson: {title}",
                                                    "committer": {"date": date}}})
        self.ci[sha] = [{"id": run_id, "name": "CI", "status": "completed", "conclusion": conclusion,
                         "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}"}]
        return sha

    def __call__(self, path: str):
        url = urllib.parse.urlparse(path)
        q = dict(urllib.parse.parse_qsl(url.query))
        if url.path.endswith("/commits"):
            hits = [c for c in self.commits if q["since"] <= c["commit"]["committer"]["date"] <= q["until"]]
            return sorted(hits, key=lambda c: c["commit"]["committer"]["date"], reverse=True)
        if "/compare/" in url.path:
            sha = url.path.rsplit("...", 1)[1]
            return {"status": "behind" if any(c["sha"] == sha for c in self.commits) else "diverged"}
        if url.path.endswith("/actions/runs"):
            return {"workflow_runs": self.ci.get(q.get("head_sha", ""), [])}
        raise ValueError(f"fixture has no answer for {path}")


def emit_runs(rec: Recorder, clock: FakeClock, gh: FixtureGitHub, start_s: float) -> None:
    for i, (title, claimed, msg, commit_min, claim_sha, ci, quality, extra) in enumerate(RUNS):
        t0 = start_s + i * 2 * 3600
        clock.set(t0)
        run = rec.run("lesson-builder", title=f"Build lesson: {title}", **{"deployment.environment": "demo"})
        clock.advance(40)
        run.llm("grok-4", input_tokens=9000, output_tokens=3500, cost_usd=0.0089, duration_ms=38000)
        clock.advance(60)
        run.tool("write_lesson_files", {"topic": title}, duration_ms=5000)
        run.tool("contrast_check", {"topic": title}, duration_ms=2000)
        for n in range(extra.get("retries", 0)):
            clock.advance(30)
            run.tool("render_images", {"topic": title}, error="upstream 503", duration_ms=20000,
                     retry=n > 0)
        if extra.get("unapproved_spend"):
            run.audit("spend", "image-generation retry credits", spend_usd=extra["unapproved_spend"])
        clock.advance(90)
        run.llm("grok-4", input_tokens=4000, output_tokens=900, cost_usd=0.0031, duration_ms=12000)
        run.audit("spend", "Guide eval (27 checks)", approval_ref="standing rule: Guide eval under 5c a run",
                  spend_usd=0.015)
        sha = None
        if commit_min is not None:
            clock.set(t0 + commit_min * 60)
            sha = gh.add(title, t0 + commit_min * 60, ci, 900000000 + i)
            run.tool("git_push", {"branch": BRANCH}, duration_ms=3000)
            run.audit("write", f"github:{REPO}@{BRANCH}", approval_ref="standing rule: visual-draft only")
            if claim_sha:
                run.claim("git_commit", summary=f"pushed lesson {title}", branch=BRANCH, revision=sha)
        for _ in range(extra.get("wait_loop", 0)):
            clock.advance(30)
            run.tool("wait_for_ci", {"job": "learn-e2e"}, duration_ms=30000)
        run.quality(quality)
        clock.set(t0 + (21 * 60 if claimed != "succeeded" else 16 * 60))
        run.end(claimed, msg)


def run_demo(db_path: str) -> Report:
    store = Store(db_path)
    server = serve(store, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        start_s = time.time() - 13 * 3600
        clock = FakeClock(start_s)
        gh = FixtureGitHub()
        rec = Recorder(f"http://127.0.0.1:{server.server_address[1]}", service_name="lesson-builder",
                       clock=clock)
        emit_runs(rec, clock, gh, start_s)
        verify_runs(store, DEMO_CONFIG, fetch=gh)
        return build_report(store.spans(), store.latest_evidence())
    finally:
        server.shutdown()
        server.server_close()
        store.close()

