from __future__ import annotations

import httpx

from job_tracker.ats import USER_AGENT
from job_tracker.models import CareerPage, JobPosting


def scrape_lever(
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

    api = f"https://api.lever.co/v0/postings/{token}?mode=json"
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

    if not isinstance(data, list):
        return []

    jobs: list[JobPosting] = []
    for item in data:
        cats = item.get("categories") or {}
        location = cats.get("location") or item.get("workplaceType")
        commitment = cats.get("commitment")
        team = cats.get("team")
        department = cats.get("department") or team

        remote = None
        wt = (item.get("workplaceType") or location or "").lower()
        if "remote" in wt:
            remote = "remote"
        elif "hybrid" in wt:
            remote = "hybrid"
        elif "onsite" in wt or "on-site" in wt:
            remote = "onsite"

        jobs.append(
            JobPosting(
                title=item.get("text") or "Untitled",
                url=item.get("hostedUrl") or item.get("applyUrl") or page.url,
                company=page.company,
                location=location,
                department=department,
                team=team,
                employment_type=commitment,
                remote=remote,
                description=item.get("descriptionPlain") or item.get("description"),
                external_id=item.get("id"),
                ats="lever",
                raw=item,
            )
        )
    return jobs
