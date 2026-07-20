from __future__ import annotations

from dataclasses import dataclass

from job_tracker.ats.ashby import scrape_ashby
from job_tracker.ats.generic import scrape_generic
from job_tracker.ats.greenhouse import scrape_greenhouse
from job_tracker.ats.lever import scrape_lever
from job_tracker.ats.smartrecruiters import scrape_smartrecruiters
from job_tracker.ats.workday import scrape_workday
from job_tracker.models import CareerPage, JobPosting

SCRAPERS = {
    "greenhouse": scrape_greenhouse,
    "lever": scrape_lever,
    "ashby": scrape_ashby,
    "workday": scrape_workday,
    "smartrecruiters": scrape_smartrecruiters,
}


@dataclass
class ScrapeFilters:
    query: str | None = None
    location: str | None = None
    department: str | None = None
    remote: str | None = None

    def active(self) -> bool:
        return bool(
            (self.query or "").strip()
            or (self.location or "").strip()
            or (self.department or "").strip()
            or (self.remote or "").strip()
        )

    def match(self, job: JobPosting) -> bool:
        return job.matches(
            query=self.query,
            location=self.location,
            department=self.department,
            remote=self.remote,
        )


def count_matches(jobs: list[JobPosting], filters: ScrapeFilters) -> int:
    if not filters.active():
        return len(jobs)
    return sum(1 for j in jobs if filters.match(j))


def scrape_jobs(
    page: CareerPage,
    *,
    filters: ScrapeFilters | None = None,
    until_match: bool = False,
) -> list[JobPosting]:
    """Scrape jobs for a resolved career page.

    If until_match is True and filters are set, paginated scrapers keep fetching
    until at least one job matches or the board is exhausted.
    """
    f = filters or ScrapeFilters()
    hunt = until_match and f.active()
    kwargs = {
        "query": f.query,
        "location": f.location,
        "department": f.department,
        "remote": f.remote,
        "until_match": hunt,
    }

    if page.ats and page.ats in SCRAPERS:
        jobs = SCRAPERS[page.ats](page, **kwargs)
        if jobs:
            return jobs
    return scrape_generic(page)


def available_filters(jobs: list[JobPosting]) -> dict[str, list[str]]:
    def uniq(values: list[str | None]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for v in values:
            if not v:
                continue
            key = v.strip()
            if not key or key.lower() in seen:
                continue
            seen.add(key.lower())
            out.append(key)
        return sorted(out, key=str.lower)

    return {
        "locations": uniq([j.location for j in jobs]),
        "departments": uniq([j.department for j in jobs]),
        "teams": uniq([j.team for j in jobs]),
        "employment_types": uniq([j.employment_type for j in jobs]),
        "remote": uniq([j.remote for j in jobs]),
    }
