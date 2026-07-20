from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from job_tracker.ats import USER_AGENT
from job_tracker.models import CareerPage, JobPosting

WD_RE = re.compile(
    r"https?://(?P<host>[A-Za-z0-9-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/"
    r"(?:(?P<locale>[a-z]{2}-[A-Z]{2})/)?"
    r"(?P<site>[A-Za-z0-9_-]+)",
    re.I,
)


def _parse_workday(page: CareerPage) -> tuple[str, str, str] | None:
    """Return (wd_host, tenant, site) or None."""
    m = WD_RE.search(page.url)
    if m:
        wd_host = f"{m.group('host')}.{m.group('wd')}.myworkdayjobs.com"
        return wd_host, m.group("host"), m.group("site")

    token = page.board_token or ""
    if "|" in token:
        tenant, site = token.split("|", 1)
        parsed = urlparse(page.url)
        host = parsed.netloc or f"{tenant}.wd5.myworkdayjobs.com"
        return host, tenant, site
    return None


def _item_to_job(item: dict, page: CareerPage, wd_host: str, site: str) -> JobPosting:
    title = item.get("title") or "Untitled"
    loc = item.get("locationsText")
    path = item.get("externalPath") or ""
    if path.startswith("/"):
        url = f"https://{wd_host}/{site}{path}"
    else:
        url = path or page.url
    remote = "remote" if loc and "remote" in loc.lower() else None
    bullets = item.get("bulletFields") or []
    return JobPosting(
        title=title,
        url=url,
        company=page.company,
        location=loc,
        department=None,
        remote=remote,
        external_id=str(bullets[-1]) if bullets else None,
        ats="workday",
        raw=item,
    )


def scrape_workday(
    page: CareerPage,
    *,
    query: str | None = None,
    location: str | None = None,
    department: str | None = None,
    remote: str | None = None,
    until_match: bool = False,
) -> list[JobPosting]:
    parsed = _parse_workday(page)
    if not parsed:
        return []

    wd_host, tenant, site = parsed
    api = f"https://{wd_host}/wday/cxs/{tenant}/{site}/jobs"

    jobs: list[JobPosting] = []
    offset = 0
    limit = 20
    found_match = False
    search_text = (query or "").strip()

    with httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        while True:
            payload = {
                "appliedFacets": {},
                "limit": limit,
                "offset": offset,
                "searchText": search_text,
            }
            resp = client.post(api, json=payload)
            if resp.status_code >= 400:
                return jobs
            data = resp.json()
            postings = data.get("jobPostings") or []
            if not postings:
                break

            for item in postings:
                job = _item_to_job(item, page, wd_host, site)
                jobs.append(job)
                if until_match and job.matches(
                    query=query,
                    location=location,
                    department=department,
                    remote=remote,
                ):
                    found_match = True

            total = int(data.get("total") or 0)
            offset += limit
            if until_match and found_match:
                break
            if offset >= total or len(postings) < limit:
                break

    return jobs
