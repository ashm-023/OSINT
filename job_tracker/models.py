from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class JobPosting:
    title: str
    url: str
    company: str
    location: str | None = None
    department: str | None = None
    team: str | None = None
    employment_type: str | None = None  # full-time, part-time, contract, intern
    remote: str | None = None  # remote, hybrid, onsite
    description: str | None = None
    external_id: str | None = None
    ats: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def matches(
        self,
        *,
        query: str | None = None,
        location: str | None = None,
        department: str | None = None,
        employment_type: str | None = None,
        remote: str | None = None,
    ) -> bool:
        def contains(hay: str | None, needle: str | None) -> bool:
            if not needle:
                return True
            if not hay:
                return False
            return needle.lower() in hay.lower()

        if query and not (
            contains(self.title, query)
            or contains(self.department, query)
            or contains(self.team, query)
            or contains(self.location, query)
        ):
            return False
        if not contains(self.location, location):
            return False
        if not contains(self.department, department) and not contains(self.team, department):
            return False
        if not contains(self.employment_type, employment_type):
            return False
        if not contains(self.remote, remote):
            return False
        return True


@dataclass
class CareerPage:
    company: str
    url: str
    ats: str | None = None
    board_token: str | None = None
    confidence: float = 0.0
    source: str = "search"
