"""One-off backfill: mirror the *existing* job history from a jobs.db into
Notion, for jobs that predate the Notion integration (scout.py only syncs
jobs it discovers on a run, not ones already in the db).

Reuses notion_sync.NotionSync.sync_job, so it shares the same dedup logic:
each synced job's Notion page id is cached in bot_state under
`notion_page:{refnr}`, so running this repeatedly (or letting scout.py sync
the same job later) never creates duplicate rows.

Usage:
    # against the local ./data/jobs.db (or whatever DB_PATH points to)
    python notion_backfill.py

    # against an explicit db copied down from the VPS
    python notion_backfill.py --db /path/to/prod/jobs.db

    # preview only, no writes to Notion
    python notion_backfill.py --db /path/to/jobs.db --dry-run

Reusing the existing Notion database:
    The "Job Scout" database is created once and its id is cached in the
    bot_state of whatever db first synced. If you created the database from
    a *different* db (e.g. the connection test wrote it to ./jobs.db but the
    real history is in ./data/jobs.db), pass --database-id so this backfill
    writes into the same Notion database instead of creating a second one:

        python notion_backfill.py --db data/jobs.db --database-id 3969c6e6-...

    The id is then seeded into the target db's bot_state, so future scout.py
    runs against that same db reuse it too.

Requires NOTION_API_KEY / NOTION_PARENT_PAGE_ID in .env (same as scout.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from analyzer import JobScore  # noqa: E402
from arbeitsagentur import Job  # noqa: E402
from notion_sync import DATABASE_ID_KEY, NotionSync, NotionSyncError  # noqa: E402
from storage import JOB_COLUMNS, JobStorage  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notion_backfill")


def _json_list(value: str | None) -> list[str]:
    """key_skills / fit_reasons / flags are stored as JSON arrays (or NULL)."""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


def _row_to_job_and_score(row: dict) -> tuple[Job, JobScore | None]:
    job = Job(
        refnr=row["refnr"],
        title=row.get("title") or "",
        employer=row.get("employer") or "",
        location=row.get("location") or "",
        posted_date=row.get("posted_date") or "",
        profession=row.get("profession") or "",
        url=row.get("url") or "",
        description=row.get("description") or "",
        # The jobs table doesn't persist which JobSource a row came from, so
        # sync_job falls back to its default ("arbeitsagentur"). Historically
        # that's what nearly all rows are; set --source to override globally.
        extra={},
    )
    # A row has a score only if it was LLM-scored at discovery time; unscored
    # rows stay unscored in Notion too (Score/Skills/Flags left empty).
    if row.get("score") is None:
        return job, None
    score = JobScore(
        score=row["score"],
        summary=row.get("summary") or "",
        key_skills=_json_list(row.get("key_skills")),
        fit_reasons=_json_list(row.get("fit_reasons")),
        flags=_json_list(row.get("flags")),
    )
    return job, score


def _load_all_jobs(storage: JobStorage) -> list[dict]:
    cols = ", ".join(JOB_COLUMNS)
    cur = storage.conn.execute(f"SELECT {cols} FROM jobs ORDER BY seen_at")
    return [dict(zip(JOB_COLUMNS, r)) for r in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--db",
        default=os.getenv("DB_PATH") or str(Path(__file__).parent / "data" / "jobs.db"),
        help="Path to the jobs.db holding the history to backfill (default: $DB_PATH or ./data/jobs.db).",
    )
    parser.add_argument(
        "--database-id",
        default=None,
        help="Existing Notion 'Job Scout' database id to sync into. Seeded "
             "into the target db's bot_state so no duplicate database is created.",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Override the Source property for every backfilled job "
             "(default: sync_job's own default, 'arbeitsagentur').",
    )
    parser.add_argument("--dry-run", action="store_true", help="List what would sync; write nothing to Notion.")
    args = parser.parse_args()

    api_key = os.getenv("NOTION_API_KEY")
    parent_page_id = os.getenv("NOTION_PARENT_PAGE_ID")
    if not api_key or not parent_page_id:
        log.error("NOTION_API_KEY and NOTION_PARENT_PAGE_ID must both be set in .env.")
        return 1

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("DB not found: %s — nothing to backfill.", db_path)
        return 1

    storage = JobStorage(db_path)
    jobs = _load_all_jobs(storage)
    log.info("Loaded %d job(s) from %s", len(jobs), db_path)
    if not jobs:
        log.info("No jobs in this db — nothing to backfill.")
        return 0

    # Seed the existing Notion database id into this db's bot_state (if given
    # and not already present) so both this backfill and future scout runs
    # against this db write into the same 'Job Scout' database.
    if args.database_id:
        cached = storage.get_bot_state(DATABASE_ID_KEY)
        if cached and cached != args.database_id:
            log.warning("bot_state already has database id %s — overwriting with --database-id %s",
                        cached, args.database_id)
        storage.set_bot_state(DATABASE_ID_KEY, args.database_id)

    already = sum(1 for j in jobs if storage.get_bot_state(f"notion_page:{j['refnr']}"))
    pending = len(jobs) - already
    log.info("%d already in Notion, %d to sync.", already, pending)

    if args.dry_run:
        for j in jobs:
            mark = "SKIP (synced)" if storage.get_bot_state(f"notion_page:{j['refnr']}") else "SYNC"
            log.info("  [%s] %s — %s (%s)", mark, j["refnr"], (j.get("title") or "")[:60], j.get("employer") or "?")
        log.info("Dry run: would sync %d job(s). No writes made.", pending)
        return 0

    synced = failed = 0
    with NotionSync(api_key=api_key, parent_page_id=parent_page_id, storage=storage) as notion:
        try:
            database_id = notion.ensure_database()
        except (NotionSyncError, Exception) as exc:  # noqa: BLE001
            log.error("Could not ensure Notion database: %s", exc)
            return 1
        log.info("Syncing into Notion database %s", database_id)

        for i, row in enumerate(jobs, 1):
            job, score = _row_to_job_and_score(row)
            if args.source:
                job.extra["source"] = args.source
            try:
                page_id = notion.sync_job(database_id, job, score)
            except (NotionSyncError, Exception) as exc:  # noqa: BLE001
                failed += 1
                log.warning("  [%d/%d] FAILED %s: %s", i, len(jobs), job.refnr, exc)
                continue
            if page_id:
                synced += 1
                log.info("  [%d/%d] synced %s — %s", i, len(jobs), job.refnr, (job.title or "")[:60])

    log.info("Backfill done: %d newly synced, %d already present, %d failed.",
             synced, already, failed)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
