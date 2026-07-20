from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from job_tracker.db import Database
from job_tracker.discovery import resolve_career_page
from job_tracker.models import CareerPage
from job_tracker.scraper import scrape_jobs

ROOT = Path(__file__).resolve().parent
env = Environment(
    loader=FileSystemLoader(str(ROOT / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)

app = FastAPI(title="Job Tracker")


def get_db() -> Database:
    return Database()


def render(name: str, **context: object) -> HTMLResponse:
    html = env.get_template(name).render(**context)
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    company: str | None = None,
    q: str | None = None,
    location: str | None = None,
    department: str | None = None,
    remote: str | None = None,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    db = get_db()
    try:
        companies = db.list_companies()
        jobs = db.query_jobs(
            company=company or None,
            query=q or None,
            location=location or None,
            department=department or None,
            remote=remote or None,
            limit=200,
        )
        filters = db.job_filters(company or None)
    finally:
        db.close()

    return render(
        "index.html",
        companies=companies,
        jobs=jobs,
        filters=filters,
        selected_company=company or "",
        q=q or "",
        location=location or "",
        department=department or "",
        remote=remote or "",
        message=message,
        error=error,
    )


@app.post("/scrape")
def scrape(
    company: str = Form(...),
    url: str = Form(""),
) -> RedirectResponse:
    company = company.strip()
    url = url.strip() or None
    if not company:
        return RedirectResponse("/?error=Enter+a+company+name", status_code=303)

    db = get_db()
    try:
        cached = db.get_company(company)
        if url:
            page = resolve_career_page(company, prefer_url=url)
        elif cached and cached.get("career_url"):
            page = CareerPage(
                company=cached["name"],
                url=cached["career_url"],
                ats=cached.get("ats"),
                board_token=cached.get("board_token"),
                confidence=1.0,
                source="db",
            )
        else:
            page = resolve_career_page(company)

        db.upsert_company(page)
        jobs = scrape_jobs(page)
        saved = db.upsert_jobs(jobs)
        msg = quote(
            f"Found {page.ats or 'generic'} board for {company}. "
            f"Scraped {len(jobs)} jobs ({saved} saved)."
        )
        return RedirectResponse(
            f"/?company={quote(company)}&message={msg}",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/?error={quote(str(exc)[:200])}",
            status_code=303,
        )
    finally:
        db.close()


@app.get("/api/jobs")
def api_jobs(
    company: str | None = Query(None),
    q: str | None = Query(None),
    location: str | None = Query(None),
    department: str | None = Query(None),
    remote: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    db = get_db()
    try:
        jobs = db.query_jobs(
            company=company,
            query=q,
            location=location,
            department=department,
            remote=remote,
            limit=limit,
        )
        filters = db.job_filters(company)
        return {"jobs": jobs, "filters": filters, "count": len(jobs)}
    finally:
        db.close()
