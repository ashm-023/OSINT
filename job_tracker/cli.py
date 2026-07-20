from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from job_tracker.db import Database
from job_tracker.discovery import resolve_career_page, search_career_pages
from job_tracker.models import JobPosting
from job_tracker.scraper import available_filters, scrape_jobs

app = typer.Typer(
    name="job-tracker",
    help="Find company career pages, scrape job postings, and filter them.",
    no_args_is_help=True,
)
console = Console()


def _db(path: Optional[Path]) -> Database:
    return Database(path)


@app.command("find")
def find_cmd(
    company: str = typer.Argument(..., help="Company name to look up"),
    url: Optional[str] = typer.Option(
        None, "--url", "-u", help="Skip search and use this careers URL"
    ),
    save: bool = typer.Option(True, "--save/--no-save", help="Save company to local DB"),
    db_path: Optional[Path] = typer.Option(None, "--db", help="SQLite DB path"),
) -> None:
    """Search the web for a company's career page and detect its ATS."""
    with console.status(f"Finding careers page for [bold]{company}[/bold]..."):
        if url:
            page = resolve_career_page(company, prefer_url=url)
            alternatives: list = []
        else:
            alternatives = search_career_pages(company)
            page = resolve_career_page(company)

    console.print(f"\n[bold green]Best match[/bold green] for [bold]{company}[/bold]")
    console.print(f"  URL:      {page.url}")
    console.print(f"  ATS:      {page.ats or 'unknown (generic scrape)'}")
    console.print(f"  Board:    {page.board_token or '-'}")
    console.print(f"  Score:    {page.confidence:.2f}")
    console.print(f"  Source:   {page.source}")

    if alternatives and len(alternatives) > 1:
        table = Table(title="Other candidates", show_lines=False)
        table.add_column("Score", justify="right")
        table.add_column("ATS")
        table.add_column("URL")
        for alt in alternatives[:5]:
            if alt.url == page.url:
                continue
            table.add_row(f"{alt.confidence:.2f}", alt.ats or "-", alt.url)
        if table.row_count:
            console.print(table)

    if save:
        db = _db(db_path)
        db.upsert_company(page)
        console.print(f"\n[dim]Saved company to {db.path}[/dim]")
        db.close()


@app.command("scrape")
def scrape_cmd(
    company: str = typer.Argument(..., help="Company name"),
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Careers URL override"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Keyword filter"),
    location: Optional[str] = typer.Option(None, "--location", "-l"),
    department: Optional[str] = typer.Option(None, "--department", "-d"),
    employment_type: Optional[str] = typer.Option(None, "--type", "-t"),
    remote: Optional[str] = typer.Option(
        None, "--remote", "-r", help="remote / hybrid / onsite"
    ),
    limit: int = typer.Option(50, "--limit", help="Max jobs to show"),
    save: bool = typer.Option(True, "--save/--no-save"),
    db_path: Optional[Path] = typer.Option(None, "--db"),
) -> None:
    """Find (or reuse) a careers page, scrape postings, apply filters, store results."""
    db = _db(db_path)

    page = None
    if url:
        with console.status("Resolving careers URL..."):
            page = resolve_career_page(company, prefer_url=url)
    else:
        cached = db.get_company(company)
        if cached and cached.get("career_url"):
            from job_tracker.models import CareerPage

            page = CareerPage(
                company=cached["name"],
                url=cached["career_url"],
                ats=cached.get("ats"),
                board_token=cached.get("board_token"),
                confidence=1.0,
                source="db",
            )
            console.print(f"[dim]Using cached careers page: {page.url}[/dim]")
        else:
            with console.status(f"Finding careers page for [bold]{company}[/bold]..."):
                page = resolve_career_page(company)

    db.upsert_company(page)

    with console.status(f"Scraping jobs via [bold]{page.ats or 'generic'}[/bold]..."):
        jobs = scrape_jobs(page)

    filtered = [
        j
        for j in jobs
        if j.matches(
            query=query,
            location=location,
            department=department,
            employment_type=employment_type,
            remote=remote,
        )
    ]

    if save:
        n = db.upsert_jobs(jobs)
        console.print(f"[green]Scraped {len(jobs)} jobs[/green] ({n} saved to DB)")
    else:
        console.print(f"[green]Scraped {len(jobs)} jobs[/green] (not saved)")

    _print_jobs(filtered[:limit])
    _print_filter_summary(available_filters(jobs))
    db.close()


@app.command("list")
def list_cmd(
    company: Optional[str] = typer.Option(None, "--company", "-c"),
    query: Optional[str] = typer.Option(None, "--query", "-q"),
    location: Optional[str] = typer.Option(None, "--location", "-l"),
    department: Optional[str] = typer.Option(None, "--department", "-d"),
    employment_type: Optional[str] = typer.Option(None, "--type", "-t"),
    remote: Optional[str] = typer.Option(None, "--remote", "-r"),
    limit: int = typer.Option(50, "--limit"),
    db_path: Optional[Path] = typer.Option(None, "--db"),
) -> None:
    """List jobs already stored in the local database."""
    db = _db(db_path)
    rows = db.query_jobs(
        company=company,
        query=query,
        location=location,
        department=department,
        employment_type=employment_type,
        remote=remote,
        limit=limit,
    )
    jobs = [
        JobPosting(
            title=r["title"],
            url=r["url"],
            company=r["company"],
            location=r["location"],
            department=r["department"],
            team=r["team"],
            employment_type=r["employment_type"],
            remote=r["remote"],
            ats=r["ats"],
        )
        for r in rows
    ]
    _print_jobs(jobs)
    db.close()


@app.command("filters")
def filters_cmd(
    company: Optional[str] = typer.Option(None, "--company", "-c"),
    db_path: Optional[Path] = typer.Option(None, "--db"),
) -> None:
    """Show available filter values from scraped jobs."""
    db = _db(db_path)
    filters = db.job_filters(company)
    _print_filter_summary(filters)
    db.close()


@app.command("companies")
def companies_cmd(
    db_path: Optional[Path] = typer.Option(None, "--db"),
) -> None:
    """List tracked companies."""
    db = _db(db_path)
    rows = db.list_companies()
    if not rows:
        console.print("[yellow]No companies tracked yet. Run: track find \"Acme\"[/yellow]")
        db.close()
        raise typer.Exit()

    table = Table(title="Tracked companies")
    table.add_column("Company")
    table.add_column("ATS")
    table.add_column("Careers URL")
    for r in rows:
        table.add_row(r["name"], r["ats"] or "-", r["career_url"] or "-")
    console.print(table)
    db.close()


@app.command("export")
def export_cmd(
    output: Path = typer.Argument(..., help="Output .csv or .json path"),
    company: Optional[str] = typer.Option(None, "--company", "-c"),
    query: Optional[str] = typer.Option(None, "--query", "-q"),
    location: Optional[str] = typer.Option(None, "--location", "-l"),
    department: Optional[str] = typer.Option(None, "--department", "-d"),
    limit: int = typer.Option(1000, "--limit"),
    db_path: Optional[Path] = typer.Option(None, "--db"),
) -> None:
    """Export filtered jobs to CSV or JSON."""
    db = _db(db_path)
    rows = db.query_jobs(
        company=company,
        query=query,
        location=location,
        department=department,
        limit=limit,
    )
    db.close()

    if not rows:
        console.print("[yellow]No jobs matched.[/yellow]")
        raise typer.Exit(code=1)

    suffix = output.suffix.lower()
    if suffix == ".json":
        output.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    elif suffix == ".csv":
        fields = [
            "company",
            "title",
            "location",
            "department",
            "team",
            "employment_type",
            "remote",
            "url",
            "ats",
            "scraped_at",
        ]
        with output.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    else:
        console.print("[red]Use a .csv or .json output path[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]Exported {len(rows)} jobs → {output}[/green]")


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Launch the web UI."""
    import uvicorn

    console.print(f"[green]Job Tracker UI:[/green] http://{host}:{port}")
    uvicorn.run("job_tracker.web:app", host=host, port=port, reload=False)


def _print_jobs(jobs: list[JobPosting]) -> None:
    if not jobs:
        console.print("[yellow]No jobs matched your filters.[/yellow]")
        return

    table = Table(title=f"Jobs ({len(jobs)})", show_lines=False)
    table.add_column("Company", style="cyan")
    table.add_column("Title", style="bold")
    table.add_column("Location")
    table.add_column("Dept")
    table.add_column("Type")
    table.add_column("Remote")
    table.add_column("URL", overflow="fold")

    for j in jobs:
        table.add_row(
            j.company,
            j.title,
            j.location or "-",
            j.department or j.team or "-",
            j.employment_type or "-",
            j.remote or "-",
            j.url,
        )
    console.print(table)


def _print_filter_summary(filters: dict[str, list[str]]) -> None:
    console.print("\n[bold]Available filters[/bold]")
    for key, values in filters.items():
        if not values:
            continue
        preview = ", ".join(values[:12])
        more = f" (+{len(values) - 12} more)" if len(values) > 12 else ""
        console.print(f"  [cyan]{key}[/cyan]: {preview}{more}")


def main() -> None:
    # Allow `python -m job_tracker` and `python main.py`
    app()


if __name__ == "__main__":
    main()
    sys.exit(0)
