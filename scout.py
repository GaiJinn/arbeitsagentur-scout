"""
arbeitsagentur-scout — main entry point.

Runs once: searches all configured queries, dedups, analyzes new jobs with
an LLM, sends a Telegram summary, and exits. Designed for cron / systemd timer.

Usage:
    python scout.py
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

from arbeitsagentur import ArbeitsagenturClient, Job
from analyzer import LLMAnalyzer, JobScore
from job_source import JobSource
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

# Minimum LLM score for a job to also get a "generate tailored CV" button.
# Requires BASE_CV_PATH (a PDF) and the telegram_bot.py listener running.
CV_SCORE_THRESHOLD = int(os.getenv("CV_SCORE_THRESHOLD", "7"))
BASE_CV_PATH = Path(os.getenv("BASE_CV_PATH") or Path(__file__).parent / "cv.pdf")

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
# Job sources
# ---------------------------------------------------------------------------
# Maps a queries.json "source" field to the JobSource implementation to use.
# arbeitsagentur.de is the only one today; adding a new portal (StepStone,
# Indeed, ...) is meant to be "write a JobSource subclass, register it here" —
# see job_source.py — without touching the pipeline below. Queries without an
# explicit "source" default to arbeitsagentur (backwards-compatible with
# existing queries.json files).
DEFAULT_SOURCE = "arbeitsagentur"
SOURCE_REGISTRY: dict[str, type[JobSource]] = {
    DEFAULT_SOURCE: ArbeitsagenturClient,
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# A short id per cron run, attached to every log line, so grepping one run's
# worth of output out of a shared log file (or journald) is a single `grep
# run=xxxxxxxx` instead of guessing timestamps.
RUN_ID = uuid.uuid4().hex[:8]


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] [run=%(run_id)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Attached to the handler (not the root logger) so it applies to records
# propagated up from every module's logger (analyzer, arbeitsagentur, ...),
# not just ones logged directly through the root logger.
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RunIdFilter(RUN_ID))
log = logging.getLogger("scout")
log.info("run id: %s", RUN_ID)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def _collect_new_jobs(
    sources: dict[str, JobSource],
    storage: JobStorage,
    analyzer: LLMAnalyzer | None,
) -> list[tuple[Job, JobScore | None]]:
    new_jobs: list[tuple[Job, JobScore | None]] = []

    for query in SEARCH_QUERIES:
        label = query["label"]
        params = query["params"]
        source_name = query.get("source", DEFAULT_SOURCE)
        client = sources.get(source_name)
        if client is None:
            log.error(
                "Query %r asks for source %r, but only %s is configured — skipping.",
                label, source_name, sorted(sources) or "(none)",
            )
            continue

        log.info("Searching [%s]: %s", source_name, label)
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
                # Pull the full Stellenbeschreibung for richer scoring. If it
                # fails, do NOT fall back to scoring off just "beruf — titel"
                # (a couple of words) — that starves the LLM of context and
                # produces an unreliable score. Skip scoring for this job
                # instead; it stays unscored ([--]  in the summary) rather
                # than silently mis-ranked.
                detail_text = ""
                try:
                    detail_text = client.fetch_details(job.refnr)
                except Exception:  # noqa: BLE001
                    log.warning("details fetch raised for %s", job.refnr)

                if detail_text:
                    job.description = detail_text
                    try:
                        score = analyzer.score(job)
                    except Exception:  # noqa: BLE001
                        log.exception("LLM scoring failed for %s", job.refnr)
                        score = None
                else:
                    log.warning(
                        "No Stellenbeschreibung available for %s (%s) — "
                        "skipping LLM scoring rather than scoring on title alone.",
                        job.refnr, job.title,
                    )
            storage.save(job, score)
            new_jobs.append((job, score))

    return new_jobs


def main() -> int:
    log.info("=== arbeitsagentur-scout run start ===")
    storage = JobStorage(DB_PATH)
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

    # Only spin up the sources actually referenced by queries.json (usually
    # just "arbeitsagentur"), so adding a second portal later doesn't require
    # credentials/config for portals you haven't configured any queries for.
    active_source_names = {q.get("source", DEFAULT_SOURCE) for q in SEARCH_QUERIES}
    unknown = active_source_names - SOURCE_REGISTRY.keys()
    if unknown:
        log.error(
            "queries.json references unknown source(s) %s — configured: %s",
            sorted(unknown), sorted(SOURCE_REGISTRY),
        )

    try:
        with contextlib.ExitStack() as stack:
            sources: dict[str, JobSource] = {
                name: stack.enter_context(cls())
                for name, cls in SOURCE_REGISTRY.items()
                if name in active_source_names
            }
            new_jobs = _collect_new_jobs(sources, storage, analyzer)

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

            if BASE_CV_PATH.exists():
                cv_candidates = [
                    (job, score) for job, score in high_value
                    if score and score.score >= CV_SCORE_THRESHOLD
                ]
                for job, score in cv_candidates:
                    notifier.send_cv_prompt(job, score)
                if cv_candidates:
                    log.info("Sent CV-generation prompts for %d jobs.", len(cv_candidates))
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
    finally:
        if notifier:
            notifier.close()


if __name__ == "__main__":
    sys.exit(main())
