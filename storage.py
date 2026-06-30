"""
storage — SQLite persistence for jobs and their scores.

Acts as the dedup layer: a refnr seen before is not processed again.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from analyzer import JobScore
from arbeitsagentur import Job

log = logging.getLogger("storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    refnr           TEXT PRIMARY KEY,
    title           TEXT,
    employer        TEXT,
    location        TEXT,
    posted_date     TEXT,
    profession      TEXT,
    url             TEXT,
    description     TEXT,
    score           INTEGER,
    summary         TEXT,
    key_skills      TEXT,
    fit_reasons     TEXT,
    flags           TEXT,
    seen_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score);
CREATE INDEX IF NOT EXISTS idx_jobs_seen_at ON jobs(seen_at);
"""


class JobStorage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def has_job(self, refnr: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM jobs WHERE refnr = ?", (refnr,))
        return cur.fetchone() is not None

    def save(self, job: Job, score: JobScore | None) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO jobs
              (refnr, title, employer, location, posted_date, profession,
               url, description, score, summary, key_skills, fit_reasons,
               flags, seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.refnr,
                job.title,
                job.employer,
                job.location,
                job.posted_date,
                job.profession,
                job.url,
                (job.description or "")[:8000],
                score.score if score else None,
                score.summary if score else None,
                json.dumps(list(score.key_skills)) if score else None,
                json.dumps(list(score.fit_reasons)) if score else None,
                json.dumps(list(score.flags)) if score else None,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()
