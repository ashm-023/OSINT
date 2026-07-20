from __future__ import annotations

import httpx

from job_tracker.ats import USER_AGENT
from job_tracker.models import CareerPage, JobPosting


def scrape_smartrecruiters(
    page: CareerPage,
    *,
    query: str | None = None,
    location: str | None = None,
    department: str | None = None,
    remote: str | None = None,
    until_match: bool = False,
) -> list[JobPosting]:
    token = page.board_token
    if not token:
        return []

    jobs: list[JobPosting] = []
    offset = 0
    limit = 100
    found_match = False

    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        while True:
            api = (
                "https://api.smartrecruiters.com/v1/companies/"
                f"{token}/postings?limit={limit}&offset={offset}"
            )
            resp = client.get(api)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content") or []
            if not content:
                break

            for item in content:
                loc = item.get("location") or {}
                location_parts = [
                    loc.get("city"),
                    loc.get("region"),
                    loc.get("country"),
                ]
                job_location = ", ".join(p for p in location_parts if p) or None
                job_remote = "remote" if loc.get("remote") else None

                dept = None
                if item.get("department"):
                    dept = item["department"].get("label")

                et = None
                if item.get("typeOfEmployment"):
                    et = item["typeOfEmployment"].get("label")

                ref = item.get("refNumber") or item.get("id")
                url = item.get("ref") or (
                    f"https://jobs.smartrecruiters.com/{token}/{ref}" if ref else page.url
                )

                job = JobPosting(
                    title=item.get("name") or "Untitled",
                    url=url,
                    company=page.company,
                    location=job_location,
                    department=dept,
                    employment_type=et,
                    remote=job_remote,
                    description=None,
                    external_id=str(item.get("id")) if item.get("id") else None,
                    ats="smartrecruiters",
                    raw=item,
                )
                jobs.append(job)
                if until_match and job.matches(
                    query=query,
                    location=location,
                    department=department,
                    remote=remote,
                ):
                    found_match = True

            total = data.get("totalFound") or 0
            offset += limit
            if until_match and found_match:
                break
            if offset >= total or len(content) < limit:
                break

    return jobs
