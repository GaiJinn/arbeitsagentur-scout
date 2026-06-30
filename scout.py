"""
arbeitsagentur-scout — main entry point.

Runs once: searches all configured queries, dedups, analyzes new jobs with
an LLM, sends a Telegram summary, and exits. Designed for cron / systemd timer.

Usage:
    python scout.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from arbeitsagentur import ArbeitsagenturClient, Job
from analyzer import LLMAnalyzer, JobScore
from notifier import TelegramNotifier
from storage import JobStorage

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# Docker compose sets DB_PATH=/data/jobs.db via env_file; local runs use ./data/jobs.db
# next to the script so a bare `python scout.py` does not need root to mkdir /data.
DEFAULT_DB_PATH = Path(__file__).parent / "data" / "jobs.db"
DB_PATH = Path(os.getenv("DB_PATH") or DEFAULT_DB_PATH)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Minimum LLM score (1-10) for a job to trigger a Telegram alert.
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "6"))

# Search queries are loaded from a local file (gitignored) so personal
# location/role preferences never live in source control. See
# queries.example.json for the format — copy it to queries.json and tune
# to your interests.
QUERIES_PATH = Path(os.getenv("QUERIES_PATH") or Path(__file__).parent / "queries.json")
if not QUERIES_PATH.exists():
    sys.exit(
        f"Search queries not found at {QUERIES_PATH}. "
        "Copy queries.example.json to queries.json and tune it, "
        "or set QUERIES_PATH to point elsewhere."
    )
SEARCH_QUERIES: list[dict] = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scout")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def main() -> int:
    log.info("=== arbeitsagentur-scout run start ===")
    storage = JobStorage(DB_PATH)
    client = ArbeitsagenturClient()
    analyzer = LLMAnalyzer(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
    notifier = (
        TelegramNotifier(token=TELEGRAM_TOKEN, chat_id=TELEGRAM_CHAT_ID)
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID
        else None
    )

    if analyzer is None:
        log.warning("GROQ_API_KEY not set — running without LLM analysis.")
    if notifier is None:
        log.warning("Telegram credentials missing — output to console only.")

    new_jobs: list[tuple[Job, JobScore | None]] = []

    for query in SEARCH_QUERIES:
        label = query["label"]
        params = query["params"]
        log.info("Searching: %s", label)
        try:
            results = client.search(**params)
        except Exception:  # noqa: BLE001
            log.exception("Search failed for %s", label)
            continue

        log.info("  → %s results from API (raw)", len(results))
        for job in results:
            if storage.has_job(job.refnr):
                continue
            score: JobScore | None = None
            if analyzer:
                # Pull full description for richer scoring.
                try:
                    detail_text = client.fetch_details(job.refnr)
                    job.description = detail_text or job.description
                except Exception:  # noqa: BLE001
                    log.warning("details fetch failed for %s", job.refnr)
                try:
                    score = analyzer.score(job)
                except Exception:  # noqa: BLE001
                    log.exception("LLM scoring failed for %s", job.refnr)
                    score = None
            storage.save(job, score)
            new_jobs.append((job, score))

    log.info("New jobs total: %d", len(new_jobs))

    if not new_jobs:
        log.info("Nothing new. Exit clean.")
        return 0

    # Filter for Telegram: only score-worthy ones.
    high_value = [
        (job, score)
        for job, score in new_jobs
        if score is None or score.score >= SCORE_THRESHOLD
    ]
    high_value.sort(key=lambda pair: (pair[1].score if pair[1] else 0), reverse=True)

    if notifier and high_value:
        notifier.send_summary(high_value, total_new=len(new_jobs))
        log.info("Telegram alert sent: %d high-value jobs.", len(high_value))
    elif not high_value:
        log.info("No jobs above threshold (%d). No alert sent.", SCORE_THRESHOLD)
    else:
        # No Telegram → console preview.
        for job, score in high_value[:10]:
            score_label = f"[{score.score}/10]" if score else "[--]"
            print(f"\n{score_label} {job.title}")
            print(f"  {job.employer} · {job.location}")
            if score:
                print(f"  → {score.summary}")
            print(f"  {job.url}")

    log.info("=== run end ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
