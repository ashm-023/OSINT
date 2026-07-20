from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from job_tracker.models import CareerPage, JobPosting

DEFAULT_DB = Path(__file__).resolve().parent.parent / "jobs.db"


class Database:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                career_url TEXT,
                ats TEXT,
                board_token TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                external_id TEXT,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                location TEXT,
                department TEXT,
                team TEXT,
                employment_type TEXT,
                remote TEXT,
                description TEXT,
                ats TEXT,
                raw_json TEXT,
                scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(company, url)
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
            CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title);
            CREATE INDEX IF NOT EXISTS idx_jobs_location ON jobs(location);
            CREATE INDEX IF NOT EXISTS idx_jobs_department ON jobs(department);
            """
        )
        self._conn.commit()

    def upsert_company(self, page: CareerPage) -> None:
        self._conn.execute(
            """
            INSERT INTO companies (name, career_url, ats, board_token)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                career_url = excluded.career_url,
                ats = excluded.ats,
                board_token = excluded.board_token,
                updated_at = CURRENT_TIMESTAMP
            """,
            (page.company, page.url, page.ats, page.board_token),
        )
        self._conn.commit()

    def get_company(self, name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM companies WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        return dict(row) if row else None

    def list_companies(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM companies ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_jobs(self, jobs: list[JobPosting]) -> int:
        count = 0
        for job in jobs:
            self._conn.execute(
                """
                INSERT INTO jobs (
                    company, external_id, title, url, location, department,
                    team, employment_type, remote, description, ats, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company, url) DO UPDATE SET
                    title = excluded.title,
                    location = excluded.location,
                    department = excluded.department,
                    team = excluded.team,
                    employment_type = excluded.employment_type,
                    remote = excluded.remote,
                    description = excluded.description,
                    ats = excluded.ats,
                    raw_json = excluded.raw_json,
                    scraped_at = CURRENT_TIMESTAMP
                """,
                (
                    job.company,
                    job.external_id,
                    job.title,
                    job.url,
                    job.location,
                    job.department,
                    job.team,
                    job.employment_type,
                    job.remote,
                    job.description,
                    job.ats,
                    json.dumps(job.raw) if job.raw else None,
                ),
            )
            count += 1
        self._conn.commit()
        return count

    def query_jobs(
        self,
        *,
        company: str | None = None,
        query: str | None = None,
        location: str | None = None,
        department: str | None = None,
        employment_type: str | None = None,
        remote: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        if company:
            clauses.append("company = ? COLLATE NOCASE")
            params.append(company)
        if query:
            clauses.append(
                "(title LIKE ? OR department LIKE ? OR team LIKE ? OR location LIKE ?)"
            )
            like = f"%{query}%"
            params.extend([like, like, like, like])
        if location:
            clauses.append("location LIKE ?")
            params.append(f"%{location}%")
        if department:
            clauses.append("(department LIKE ? OR team LIKE ?)")
            params.extend([f"%{department}%", f"%{department}%"])
        if employment_type:
            clauses.append("employment_type LIKE ?")
            params.append(f"%{employment_type}%")
        if remote:
            clauses.append("remote LIKE ?")
            params.append(f"%{remote}%")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT * FROM jobs
            {where}
            ORDER BY scraped_at DESC, title COLLATE NOCASE
            LIMIT ?
        """
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def job_filters(self, company: str | None = None) -> dict[str, list[str]]:
        """Distinct filter values currently in the DB."""
        clauses = [
            "{column} IS NOT NULL",
            "TRIM({column}) != ''",
        ]
        params: list[Any] = []
        if company:
            clauses.insert(0, "company = ? COLLATE NOCASE")
            params.append(company)

        def distinct(column: str) -> list[str]:
            where_sql = " AND ".join(c.format(column=column) for c in clauses)
            rows = self._conn.execute(
                f"""
                SELECT DISTINCT {column} AS v FROM jobs
                WHERE {where_sql}
                ORDER BY v COLLATE NOCASE
                """,
                params,
            ).fetchall()
            return [r["v"] for r in rows]

        return {
            "locations": distinct("location"),
            "departments": distinct("department"),
            "teams": distinct("team"),
            "employment_types": distinct("employment_type"),
            "remote": distinct("remote"),
        }

    def close(self) -> None:
        self._conn.close()
