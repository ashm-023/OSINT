from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

from job_tracker.models import CareerPage

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

CAREER_HINTS = (
    "career",
    "careers",
    "jobs",
    "job",
    "join-us",
    "joinus",
    "work-with-us",
    "opportunities",
    "vacancies",
    "hiring",
    "openings",
)

SKIP_HOSTS = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "glassdoor.ca",
    "facebook.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "wikipedia.org",
    "gotocareer.io",
    "beenremote.com",
    "levels.fyi",
    "teamblind.com",
    "ziprecruiter.com",
    "support.greenhouse.io",
    "www.ashbyhq.com",
    "www.lever.co",
    "www.greenhouse.io",
    "www.greenhouse.com",
    "apify.com",
)

INVALID_TOKENS = {
    "hc",
    "embed",
    "job_board",
    "js",
    "v1",
    "v0",
    "boards",
    "careers",
    "jobs",
    "postings",
    "posting-api",
    "job-board",
}

ATS_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "greenhouse",
        re.compile(
            r"(?:boards(?:-api)?\.greenhouse\.io/(?:embed/job_board/js/)?|"
            r"job-boards\.greenhouse\.io/)"
            r"(?P<token>[A-Za-z0-9_-]+)",
            re.I,
        ),
        "token",
    ),
    (
        "lever",
        re.compile(r"(?:jobs|api)\.lever\.co/(?P<token>[A-Za-z0-9_-]+)", re.I),
        "token",
    ),
    (
        "ashby",
        re.compile(
            r"(?:jobs|api)\.ashbyhq\.com/(?P<token>[A-Za-z0-9_-]+)",
            re.I,
        ),
        "token",
    ),
    (
        "smartrecruiters",
        re.compile(
            r"(?:jobs|careers)\.smartrecruiters\.com/(?P<token>[A-Za-z0-9_-]+)",
            re.I,
        ),
        "token",
    ),
    (
        "workday",
        re.compile(
            r"(?P<host>[A-Za-z0-9-]+)\.wd\d+\.myworkdayjobs\.com/"
            r"(?:[a-z]{2}-[A-Z]{2}/)?(?P<token>[A-Za-z0-9_-]+)",
            re.I,
        ),
        "host_token",
    ),
]


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"},
        follow_redirects=True,
        timeout=20.0,
    )


def _slug(company: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", company.lower())


def _score_url(url: str, company: str) -> float:
    u = url.lower()
    host = urlparse(u).netloc
    if any(s in host for s in SKIP_HOSTS):
        return -1.0

    company_slug = _slug(company)
    score = 0.0
    if any(h in u for h in CAREER_HINTS):
        score += 0.45
    if company_slug and company_slug in re.sub(r"[^a-z0-9]+", "", u):
        score += 0.25
    if any(
        x in u
        for x in (
            "greenhouse",
            "lever.co",
            "ashbyhq",
            "myworkdayjobs",
            "smartrecruiters",
        )
    ):
        score += 0.35
    if u.endswith(".pdf"):
        score -= 0.5
    return score


def detect_ats(url: str, html: str | None = None) -> tuple[str | None, str | None]:
    haystack = url
    if html:
        haystack = f"{url}\n{html}"

    for ats, pattern, kind in ATS_PATTERNS:
        m = pattern.search(haystack)
        if not m:
            continue
        if kind == "token":
            token = m.group("token")
            if token.lower() in INVALID_TOKENS:
                continue
            return ats, token
        if kind == "host_token":
            token = m.group("token")
            if token.lower() in INVALID_TOKENS:
                continue
            return ats, f"{m.group('host')}|{token}"
    return None, None


def _extract_career_links(base_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = (a.get_text(" ", strip=True) or "").lower()
        full = urljoin(base_url, href)
        blob = f"{href} {text}".lower()
        if any(h in blob for h in CAREER_HINTS):
            links.append(full)
    seen: set[str] = set()
    out: list[str] = []
    for link in links:
        key = link.split("#")[0].rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append(link)
    return out


def _guess_ats_boards(company: str) -> list[CareerPage]:
    """Probe common public ATS board URLs for the company slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    slug_compact = _slug(company)
    guesses = [
        ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", slug),
        (
            "greenhouse",
            f"https://boards-api.greenhouse.io/v1/boards/{slug_compact}/jobs",
            slug_compact,
        ),
        ("lever", f"https://api.lever.co/v0/postings/{slug}?mode=json", slug),
        (
            "lever",
            f"https://api.lever.co/v0/postings/{slug_compact}?mode=json",
            slug_compact,
        ),
        (
            "ashby",
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            slug,
        ),
        (
            "ashby",
            f"https://api.ashbyhq.com/posting-api/job-board/{slug_compact}",
            slug_compact,
        ),
    ]

    pages: list[CareerPage] = []
    seen_tokens: set[str] = set()
    with _client() as client:
        for ats, api_url, token in guesses:
            key = f"{ats}:{token}"
            if key in seen_tokens:
                continue
            seen_tokens.add(key)
            try:
                resp = client.get(api_url)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                ok = False
                if ats == "greenhouse" and isinstance(data.get("jobs"), list) and data["jobs"]:
                    ok = True
                elif ats == "lever" and isinstance(data, list) and data:
                    ok = True
                elif ats == "ashby" and isinstance(data.get("jobs"), list) and data["jobs"]:
                    ok = True
                # Empty boards still count if API is valid JSON shape
                elif ats == "greenhouse" and "jobs" in data:
                    ok = True
                elif ats == "lever" and isinstance(data, list):
                    ok = True
                elif ats == "ashby" and "jobs" in data:
                    ok = True
                if not ok:
                    continue
                public = {
                    "greenhouse": f"https://boards.greenhouse.io/{token}",
                    "lever": f"https://jobs.lever.co/{token}",
                    "ashby": f"https://jobs.ashbyhq.com/{token}",
                }[ats]
                pages.append(
                    CareerPage(
                        company=company,
                        url=public,
                        ats=ats,
                        board_token=token,
                        confidence=0.9,
                        source="ats-probe",
                    )
                )
            except Exception:
                continue
    return pages


def search_career_pages(company: str, max_results: int = 8) -> list[CareerPage]:
    """Search the web for a company's career / jobs page."""
    queries = [
        f"{company} careers",
        f"{company} jobs careers",
        f"{company} greenhouse OR lever OR ashby careers",
    ]
    candidates: dict[str, CareerPage] = {}

    try:
        with DDGS() as ddgs:
            for q in queries:
                try:
                    results = list(ddgs.text(q, max_results=max_results))
                except Exception:
                    continue
                for r in results:
                    url = (r.get("href") or r.get("link") or "").strip()
                    if not url.startswith("http"):
                        continue
                    score = _score_url(url, company)
                    if score < 0:
                        continue
                    title = r.get("title") or ""
                    if "career" in title.lower() or "job" in title.lower() or "hiring" in title.lower():
                        score += 0.1
                    key = url.split("#")[0].rstrip("/")
                    existing = candidates.get(key)
                    if not existing or score > existing.confidence:
                        ats, token = detect_ats(url)
                        candidates[key] = CareerPage(
                            company=company,
                            url=key,
                            ats=ats,
                            board_token=token,
                            confidence=score,
                            source="search",
                        )
    except Exception:
        pass

    # Always also probe known ATS board naming conventions
    for page in _guess_ats_boards(company):
        key = page.url.rstrip("/")
        existing = candidates.get(key)
        if not existing or page.confidence > existing.confidence:
            candidates[key] = page

    ranked = sorted(candidates.values(), key=lambda c: c.confidence, reverse=True)
    return [c for c in ranked if c.confidence >= 0.2][:10]


def resolve_career_page(company: str, prefer_url: str | None = None) -> CareerPage:
    """Pick the best career page for a company, enriching with ATS detection."""
    if prefer_url:
        page = CareerPage(company=company, url=prefer_url, confidence=1.0, source="manual")
        return enrich_career_page(page)

    pages = search_career_pages(company)
    if not pages:
        raise RuntimeError(
            f"Could not find a careers page for '{company}'. "
            f'Try: python main.py find "{company}" --url https://...'
        )

    # Prefer a known ATS board when available; dedupe by board token
    ats_pages = [p for p in pages if p.ats and p.board_token]
    if ats_pages:
        # Prefer probe/board roots, then highest confidence
        ats_pages.sort(
            key=lambda p: (
                1 if p.source == "ats-probe" else 0,
                p.confidence,
                0 if "/jobs/" in p.url else 1,
            ),
            reverse=True,
        )
        best = ats_pages[0]
    else:
        best = pages[0]

    try:
        best = enrich_career_page(best)
        if not best.ats:
            with _client() as client:
                resp = client.get(best.url)
                if resp.status_code < 400:
                    for link in _extract_career_links(str(resp.url), resp.text)[:8]:
                        child = enrich_career_page(
                            CareerPage(
                                company=company,
                                url=link,
                                confidence=best.confidence + 0.05,
                                source="homepage-link",
                            )
                        )
                        if child.ats:
                            return child
                        if _score_url(child.url, company) > best.confidence:
                            best = child
    except Exception:
        pass

    return best


def _normalize_ats_url(ats: str | None, token: str | None, url: str) -> str:
    if not ats or not token:
        return url
    if ats == "greenhouse":
        return f"https://boards.greenhouse.io/{token}"
    if ats == "lever":
        return f"https://jobs.lever.co/{token}"
    if ats == "ashby":
        return f"https://jobs.ashbyhq.com/{token}"
    if ats == "smartrecruiters":
        return f"https://jobs.smartrecruiters.com/{token}"
    return url


def enrich_career_page(page: CareerPage) -> CareerPage:
    """Fetch the page and detect ATS / board token from URL + HTML."""
    ats, token = detect_ats(page.url)
    html = None
    final_url = page.url
    try:
        with _client() as client:
            resp = client.get(page.url)
            final_url = str(resp.url)
            html = resp.text
            if not ats:
                ats, token = detect_ats(final_url, html)
            if not ats and html:
                soup = BeautifulSoup(html, "lxml")
                blobs = [final_url, html]
                for tag in soup.find_all(["script", "iframe", "a"]):
                    for attr in ("src", "href", "data-url"):
                        val = tag.get(attr)
                        if val:
                            blobs.append(urljoin(final_url, val))
                ats, token = detect_ats("\n".join(blobs))
    except Exception:
        pass

    ats = ats or page.ats
    token = token or page.board_token
    page.ats = ats
    page.board_token = token
    page.url = _normalize_ats_url(ats, token, final_url.split("#")[0].rstrip("/"))
    if page.ats:
        page.confidence = max(page.confidence, 0.85)
    return page
