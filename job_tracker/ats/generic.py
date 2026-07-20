from __future__ import annotations

import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from job_tracker.ats import USER_AGENT
from job_tracker.models import CareerPage, JobPosting

JOB_HREF = re.compile(
    r"(job|jobs|career|careers|position|opening|posting|/gh_jid=|/jobs/)",
    re.I,
)


def scrape_generic(page: CareerPage) -> list[JobPosting]:
    """Best-effort HTML scrape when no known ATS API is available."""
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        resp = client.get(page.url)
        resp.raise_for_status()
        html = resp.text
        final = str(resp.url)

    soup = BeautifulSoup(html, "lxml")
    jobs: list[JobPosting] = []
    seen: set[str] = set()

    # Prefer structured list items that look like postings
    candidates = soup.select(
        "a[href*='job'], a[href*='career'], a[href*='position'], "
        "a[href*='opening'], li a, article a, .job a, .opening a, "
        "[data-job] a, [class*='job'] a, [class*='opening'] a"
    )
    if not candidates:
        candidates = soup.find_all("a", href=True)

    for a in candidates:
        href = (a.get("href") or "").strip()
        title = a.get_text(" ", strip=True)
        if not href or not title or len(title) < 3 or len(title) > 180:
            continue
        if not JOB_HREF.search(href) and not JOB_HREF.search(title):
            continue
        # Skip nav noise
        if title.lower() in {"careers", "jobs", "view all", "see all jobs", "search"}:
            continue

        url = urljoin(final, href).split("#")[0]
        if url in seen or url.rstrip("/") == final.rstrip("/"):
            continue
        seen.add(url)

        location = None
        parent = a.find_parent(["li", "article", "div", "tr"])
        if parent:
            text = parent.get_text(" ", strip=True)
            # crude location heuristic: leftover text after title
            leftover = text.replace(title, "", 1).strip(" -|•·\n\t")
            if 2 < len(leftover) < 80:
                location = leftover

        remote = None
        blob = f"{title} {location or ''}".lower()
        if "remote" in blob:
            remote = "remote"
        elif "hybrid" in blob:
            remote = "hybrid"

        jobs.append(
            JobPosting(
                title=title,
                url=url,
                company=page.company,
                location=location,
                remote=remote,
                ats="generic",
            )
        )
        if len(jobs) >= 200:
            break

    return jobs
