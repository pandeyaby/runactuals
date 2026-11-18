"""Evidence adapters check a run's claimed outcome against the outside world.

An adapter has a ``name`` and ``check(run) -> Evidence``. It must never trust
the run's own status; it looks for proof (a commit, a green check, a receipt).
"""

from __future__ import annotations

from .base import Evidence
from .github import GitHubCommitCI

__all__ = ["ADAPTERS", "Evidence", "GitHubCommitCI", "build_adapter"]


ADAPTERS = {GitHubCommitCI.name: GitHubCommitCI}


def build_adapter(spec: dict, **kwargs):
    spec = dict(spec)
    kind = spec.pop("adapter")
    if kind not in ADAPTERS:
        raise ValueError(f"unknown evidence adapter {kind!r}; known: {sorted(ADAPTERS)}")
    return ADAPTERS[kind](**spec, **kwargs)
