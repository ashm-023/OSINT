from __future__ import annotations

import httpx

from job_tracker.ats import USER_AGENT
from job_tracker.models import CareerPage, JobPosting


def scrape_ashby(
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

    api = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        resp = client.get(api)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()

    jobs: list[JobPosting] = []
    for item in data.get("jobs", []):
        location = item.get("location")
        if not location and item.get("address"):
            addr = item["address"]
            location = addr.get("postalAddress") or addr.get("addressLocality")

        remote = None
        wt = (item.get("workplaceType") or "").lower()
        if "remote" in wt:
            remote = "remote"
        elif "hybrid" in wt:
            remote = "hybrid"
        elif "onsite" in wt or "office" in wt:
            remote = "onsite"
        elif location and "remote" in location.lower():
            remote = "remote"

        jobs.append(
            JobPosting(
                title=item.get("title") or "Untitled",
                url=item.get("jobUrl") or item.get("applyUrl") or page.url,
                company=page.company,
                location=location,
                department=item.get("department"),
                team=item.get("team"),
                employment_type=item.get("employmentType"),
                remote=remote,
                description=item.get("descriptionHtml") or item.get("descriptionPlain"),
                external_id=item.get("id"),
                ats="ashby",
                raw=item,
            )
        )
    return jobs
