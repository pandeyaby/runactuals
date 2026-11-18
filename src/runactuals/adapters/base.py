from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Evidence:
    adapter: str
    status: str  # verified | refuted | pending | unknown
    summary: str
    url: str = ""
    details: dict = field(default_factory=dict)
