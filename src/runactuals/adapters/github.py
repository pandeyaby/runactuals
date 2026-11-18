"""Evidence: a commit landed on a branch, and its GitHub Actions runs are green.

Works whether or not the run reported a SHA. If the run claimed a revision
(``vcs.ref.head.revision`` on a ``runactuals.claim`` span) that SHA is checked.
If it did not (the common case when a run is killed mid-way), the adapter looks
for a commit on the branch inside the run's time window, optionally matching a
message regex. That is exactly how a run marked "failed" gets caught having
actually shipped.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from .. import conventions as c
from .base import Evidence

OK_CONCLUSIONS = {"success", "skipped", "neutral"}

Fetch = Callable[[str], object]


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_fetch(api: str, token_env: str = "GITHUB_TOKEN") -> Fetch:
    def fetch(path: str):
        req = urllib.request.Request(api.rstrip("/") + path, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "runactuals",
            "X-GitHub-Api-Version": "2022-11-28"})
        token = os.environ.get(token_env)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    return fetch


class GitHubCommitCI:
    name = "github_commit_ci"

    def __init__(self, repo: str, branch: str, message_regex: str | None = None,
                 workflows: list[str] | None = None, grace_minutes: float = 30,
                 api: str = "https://api.github.com", token_env: str = "GITHUB_TOKEN",
                 fetch: Fetch | None = None, now: Callable[[], float] = time.time):
        self.repo, self.branch = repo, branch
        self.message_re = re.compile(message_regex) if message_regex else None
        self.workflows = set(workflows or [])
        self.grace_ns = int(grace_minutes * 60e9)
        self.fetch = fetch or default_fetch(api, token_env)
        self.now = now

    # -- helpers
    def _claimed_sha(self, run) -> str | None:
        for s in run.claims:
            a = s["attributes"]
            if a.get(c.VCS_REF_HEAD_REVISION) and a.get(c.VCS_REF_HEAD_NAME, self.branch) == self.branch:
                return str(a[c.VCS_REF_HEAD_REVISION])
        return None

    def _find_commit(self, run) -> dict | None:
        q = urllib.parse.urlencode({"sha": self.branch, "since": _iso(run.start_ns),
                                    "until": _iso(run.end_ns + self.grace_ns), "per_page": 100})
        commits = self.fetch(f"/repos/{self.repo}/commits?{q}") or []
        for cm in commits:  # newest first
            msg = (cm.get("commit") or {}).get("message", "")
            if not self.message_re or self.message_re.search(msg):
                return cm
        return None

    def _on_branch(self, sha: str) -> bool:
        cmp = self.fetch(f"/repos/{self.repo}/compare/{urllib.parse.quote(self.branch)}...{sha}") or {}
        return cmp.get("status") in ("identical", "behind")

    def _ci(self, sha: str) -> list[dict]:
        data = self.fetch(f"/repos/{self.repo}/actions/runs?head_sha={sha}&per_page=100") or {}
        runs = data.get("workflow_runs", [])
        if self.workflows:
            runs = [r for r in runs if r.get("name") in self.workflows]
        return runs

    # -- the check
    def check(self, run) -> Evidence:
        try:
            return self._check(run)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return Evidence(self.name, "unknown", f"GitHub lookup failed: {type(exc).__name__}",
                            details={"error": str(exc)[:200]})

    def _check(self, run) -> Evidence:
        window_closed = self.now() * 1e9 > run.end_ns + self.grace_ns
        sha = self._claimed_sha(run)
        source = "claimed"
        if sha:
            if not self._on_branch(sha):
                return Evidence(self.name, "refuted",
                                f"claimed commit {sha[:8]} is not on {self.branch}",
                                details={"sha": sha, "source": source})
        else:
            source = "found in run window"
            cm = self._find_commit(run)
            if not cm:
                status = "refuted" if window_closed else "pending"
                return Evidence(self.name, status,
                                f"no matching commit on {self.branch} during the run",
                                details={"source": source})
            sha = cm["sha"]
        commit_url = f"https://github.com/{self.repo}/commit/{sha}"
        ci = self._ci(sha)
        details = {"sha": sha, "source": source, "branch": self.branch,
                   "ci": [{"id": r.get("id"), "name": r.get("name"), "status": r.get("status"),
                           "conclusion": r.get("conclusion"), "url": r.get("html_url")} for r in ci]}
        if not ci:
            return Evidence(self.name, "refuted" if window_closed else "pending",
                            f"commit {sha[:8]} on {self.branch}, but no CI run found",
                            commit_url, details)
        bad = [r for r in ci if r.get("status") == "completed" and r.get("conclusion") not in OK_CONCLUSIONS]
        if bad:
            return Evidence(self.name, "refuted",
                            f"commit {sha[:8]} on {self.branch}; CI {bad[0].get('name')} "
                            f"{bad[0].get('conclusion')} (run {bad[0].get('id')})",
                            bad[0].get("html_url") or commit_url, details)
        if any(r.get("status") != "completed" for r in ci):
            return Evidence(self.name, "pending", f"commit {sha[:8]} on {self.branch}; CI still running",
                            commit_url, details)
        ids = ", ".join(str(r.get("id")) for r in ci)
        return Evidence(self.name, "verified",
                        f"commit {sha[:8]} on {self.branch} ({source}); CI green (run {ids})",
                        commit_url, details)
