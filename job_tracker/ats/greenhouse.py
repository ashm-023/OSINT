from __future__ import annotations

import re

import httpx

from job_tracker.ats import USER_AGENT
from job_tracker.models import CareerPage, JobPosting

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text)).strip() or None


def scrape_greenhouse(
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

    api = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
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
        location = None
        if item.get("location"):
            location = item["location"].get("name")
        departments = item.get("departments") or []
        dept = departments[0]["name"] if departments else None
        offices = item.get("offices") or []
        if not location and offices:
            location = offices[0].get("name")

        remote = None
        loc_l = (location or "").lower()
        if "remote" in loc_l:
            remote = "remote"
        elif "hybrid" in loc_l:
            remote = "hybrid"

        jobs.append(
            JobPosting(
                title=item.get("title") or "Untitled",
                url=item.get("absolute_url") or page.url,
                company=page.company,
                location=location,
                department=dept,
                employment_type=None,
                remote=remote,
                description=_strip_html(item.get("content")),
                external_id=str(item.get("id")) if item.get("id") is not None else None,
                ats="greenhouse",
                raw=item,
            )
        )
    return jobs
