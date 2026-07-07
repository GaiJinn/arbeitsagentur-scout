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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from arbeitsagentur import ArbeitsagenturClient, Job
from analyzer import LLMAnalyzer, JobScore
from ats_sources import GreenhouseSource, LeverSource, PersonioSource
from job_source import JobSource
from notifier import TelegramNotifier
from notion_sync import NotionSync
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

# Optional: mirror every new job into a Notion database (see notion_sync.py
# and README "Sync to Notion"). Both unset (default) → skipped entirely,
# same "if analyzer/notifier is None, just don't do that part" pattern as
# GROQ_API_KEY/TELEGRAM_TOKEN above.
NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID")

# Minimum LLM score (1-10) for a job to trigger a Telegram alert.
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "6"))

# Minimum LLM score for a job to also get a "generate tailored CV" button.
# Requires BASE_CV_PATH (a PDF) and the telegram_bot.py listener running.
CV_SCORE_THRESHOLD = int(os.getenv("CV_SCORE_THRESHOLD", "7"))
BASE_CV_PATH = Path(os.getenv("BASE_CV_PATH") or Path(__file__).parent / "cv.pdf")

# Dead-man's switch. scout runs on a schedule but stays silent when there's
# nothing above threshold — which is indistinguishable from a crashed cron,
# a dead container, or a revoked token. To make "no news" mean "good news"
# rather than "is it even alive?", send a short heartbeat ping on an
# otherwise-silent run, but at most once per HEARTBEAT_HOURS so it doesn't
# become noise. 0 disables it. State (last ping time) lives in the shared db.
HEARTBEAT_HOURS = int(os.getenv("HEARTBEAT_HOURS", "24"))
HEARTBEAT_STATE_KEY = "last_heartbeat"

# Safety valve on Groq usage: one LLM call per newly-seen job with a
# description, so a broad query or a first run over an empty db could fire
# hundreds of calls and blow the free-tier quota in a single run. Cap the
# calls per run; jobs beyond the cap are left unsaved so a later run (with
# fresh quota) picks them up instead of alerting on them unscored. 0 = no cap.
MAX_LLM_CALLS_PER_RUN = int(os.getenv("MAX_LLM_CALLS_PER_RUN", "50"))

# Optional external dead-man's switch (e.g. healthchecks.io). The in-process
# heartbeat can't report a *total* scout crash — it dies with the process.
# An external monitor that alerts when the ping stops does: scout GETs this
# URL on a clean run and URL + "/fail" on a crash; the monitor's own timeout
# catches "never pinged at all" (cron stopped, VPS down, import error). Empty
# disables it (default), preserving prior behaviour.
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL", "").strip()

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
def _validate_queries(queries: object) -> list[dict]:
    """Fail fast with a clear message on a malformed queries.json, instead of
    a raw KeyError/AttributeError deep inside the pipeline mid-run."""
    if not isinstance(queries, list):
        sys.exit(f"{QUERIES_PATH}: top level must be a JSON array of query objects.")
    for i, q in enumerate(queries):
        where = f"{QUERIES_PATH} entry #{i}"
        if not isinstance(q, dict):
            sys.exit(f"{where}: must be an object, got {type(q).__name__}.")
        if not isinstance(q.get("label"), str) or not q["label"].strip():
            sys.exit(f"{where}: missing required non-empty string \"label\".")
        if not isinstance(q.get("params"), dict):
            sys.exit(f"{where} ({q.get('label')!r}): missing required object \"params\".")
        if "source" in q and not isinstance(q["source"], str):
            sys.exit(f"{where} ({q['label']!r}): \"source\" must be a string.")
        if "region" in q and not isinstance(q["region"], str):
            sys.exit(f"{where} ({q['label']!r}): \"region\" must be a string.")
        if "push" in q and not isinstance(q["push"], bool):
            sys.exit(f"{where} ({q['label']!r}): \"push\" must be true or false.")
    return queries


SEARCH_QUERIES: list[dict] = _validate_queries(
    json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
)

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
    # Public, no-auth ATS feeds — see ats_sources.py module docstring for why
    # these three specifically (official public APIs, not scraping).
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "personio": PersonioSource,
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
# httpx logs every request at INFO — and the Telegram bot token is in the
# sendMessage/getUpdates URL path, so that would write the token in plaintext
# into the log file on every call. Quiet httpx to WARNING so secrets don't
# leak into logs (scout already logs its own per-query search lines).
logging.getLogger("httpx").setLevel(logging.WARNING)
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
    llm_calls = 0

    for query in SEARCH_QUERIES:
        label = query["label"]
        params = query["params"]
        source_name = query.get("source", DEFAULT_SOURCE)
        # City bucket for the Notion by-city trend view, and whether hits from
        # this query may trigger a Telegram push. Trend-only metros set
        # push:false — scored + mirrored to Notion but never alerted.
        region = query.get("region", "")
        push = query.get("push", True)
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
            # Stamped on the in-memory Job so a later notion_sync.sync_job
            # call (same run, same new_jobs list) knows which JobSource this
            # came from — not persisted to SQLite, only needed within this run.
            job.extra["source"] = source_name
            job.extra["region"] = region
            job.extra["push"] = push
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
                    if MAX_LLM_CALLS_PER_RUN and llm_calls >= MAX_LLM_CALLS_PER_RUN:
                        # Over budget for this run — leave the job unsaved so a
                        # later run (fresh quota) scores it, rather than saving
                        # it unscored and alerting on it with no score.
                        log.warning(
                            "LLM call cap (%d) reached — deferring %s to a later run.",
                            MAX_LLM_CALLS_PER_RUN, job.refnr,
                        )
                        continue
                    job.description = detail_text
                    llm_calls += 1
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ping_healthcheck(suffix: str = "") -> None:
    """Best-effort ping to an external uptime monitor (healthchecks.io etc).
    Never let a monitoring hiccup affect the run itself — swallow all errors."""
    if not HEALTHCHECK_URL:
        return
    try:
        httpx.get(HEALTHCHECK_URL + suffix, timeout=10.0)
    except httpx.HTTPError as exc:
        log.warning("Healthcheck ping failed (%s): %s", suffix or "success", exc)


def _handle_heartbeat(
    notifier: TelegramNotifier | None,
    storage: JobStorage,
    *,
    alert_sent: bool,
    quiet: bool,
    total_new: int,
) -> None:
    """Dead-man's switch.

    A real alert already proves the scout is alive, so an alert-sent run just
    resets the timer. On a genuinely quiet run (nothing above threshold),
    send a short "still running" ping — but no more than once per
    HEARTBEAT_HOURS — so a silent scout is distinguishable from "just no new
    jobs". `quiet` is False when there *were* high-value jobs but the send
    failed: we deliberately don't paper that over with an all-good ping.
    """
    if alert_sent:
        storage.set_bot_state(HEARTBEAT_STATE_KEY, _now_iso())
        return
    if notifier is None or HEARTBEAT_HOURS <= 0 or not quiet:
        return

    last = storage.get_bot_state(HEARTBEAT_STATE_KEY)
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            last_dt = None
        if last_dt is not None and (
            datetime.now(timezone.utc) - last_dt < timedelta(hours=HEARTBEAT_HOURS)
        ):
            return

    text = (
        "✅ <b>scout läuft</b> — keine neuen Treffer über Schwelle "
        f"{SCORE_THRESHOLD}.\n<i>{total_new} neue Stelle(n) geprüft.</i>"
    )
    if notifier.send_text(text):
        storage.set_bot_state(HEARTBEAT_STATE_KEY, _now_iso())
        log.info("Heartbeat ping sent (no jobs above threshold).")


def main() -> int:
    log.info("=== arbeitsagentur-scout run start ===")
    storage = JobStorage(DB_PATH)
    analyzer = LLMAnalyzer(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
    notifier = (
        TelegramNotifier(token=TELEGRAM_TOKEN, chat_id=TELEGRAM_CHAT_ID)
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID
        else None
    )
    notion = (
        NotionSync(api_key=NOTION_API_KEY, parent_page_id=NOTION_PARENT_PAGE_ID, storage=storage)
        if NOTION_API_KEY and NOTION_PARENT_PAGE_ID
        else None
    )

    if analyzer is None:
        log.warning("GROQ_API_KEY not set — running without LLM analysis.")
    if notifier is None:
        log.warning("Telegram credentials missing — output to console only.")
    if notion is None and (NOTION_API_KEY or NOTION_PARENT_PAGE_ID):
        # Only one of the two set is almost certainly a config mistake, not
        # an intentional "feature off" — worth a louder warning than silence.
        log.warning("NOTION_API_KEY and NOTION_PARENT_PAGE_ID must both be set — Notion sync disabled.")

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
            log.info("Nothing new this run.")

        # Mirror the full new-jobs history into Notion — not just the
        # high-value ones below, same "keep everything, filter in the
        # viewer" philosophy as the SQLite db / Streamlit dashboard. Never
        # allowed to affect the Telegram alert path: a Notion outage is
        # logged and swallowed inside sync_new_jobs, not raised here.
        if notion and new_jobs:
            synced = notion.sync_new_jobs(new_jobs)
            log.info("Notion sync: %d/%d new jobs synced.", synced, len(new_jobs))

        # Filter for Telegram: only push-eligible (Düsseldorf) *and* score-worthy
        # ones (unscored jobs pass the score test too). Trend-only metros
        # (push:false) are excluded here so they never alert — they still live
        # in Notion for the by-city trend view.
        high_value = [
            (job, score)
            for job, score in new_jobs
            if job.extra.get("push", True)
            and (score is None or score.score >= SCORE_THRESHOLD)
        ]
        high_value.sort(key=lambda pair: (pair[1].score if pair[1] else 0), reverse=True)

        alert_sent = False
        if notifier and high_value:
            if notifier.send_summary(high_value, total_new=len(new_jobs)):
                alert_sent = True
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
            else:
                # Previously the send result was ignored and this path still
                # logged "alert sent" — a Telegram 400 (bad chat_id, malformed
                # HTML, over-long message) looked green in the logs but never
                # reached the phone. Now it's a loud error instead of a silent
                # false success.
                log.error(
                    "Telegram send FAILED for %d high-value jobs — check "
                    "TELEGRAM_TOKEN / TELEGRAM_CHAT_ID and message formatting.",
                    len(high_value),
                )
        elif not notifier and high_value:
            # No Telegram configured → console preview.
            for job, score in high_value[:10]:
                score_label = f"[{score.score}/10]" if score else "[--]"
                print(f"\n{score_label} {job.title}")
                print(f"  {job.employer} · {job.location}")
                if score:
                    print(f"  → {score.summary}")
                print(f"  {job.url}")
        else:
            log.info("No jobs above threshold (%d). No alert sent.", SCORE_THRESHOLD)

        # Dead-man's switch: keep silence meaningful (see _handle_heartbeat).
        _handle_heartbeat(
            notifier, storage,
            alert_sent=alert_sent,
            quiet=not high_value,
            total_new=len(new_jobs),
        )

        _ping_healthcheck()  # clean run — tell the external monitor we're alive
        log.info("=== run end ===")
        return 0
    except Exception:
        # A crash means the external monitor should hear about it now, rather
        # than only inferring it later from a missing success ping.
        _ping_healthcheck("/fail")
        raise
    finally:
        if notifier:
            notifier.close()
        if notion:
            notion.close()


if __name__ == "__main__":
    sys.exit(main())
