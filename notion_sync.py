"""
notion_sync — mirror new jobs into a Notion database, so job history is
browsable in Notion (grouped/board views by Employer, Source, Score, ...)
instead of (or alongside) the Streamlit dashboard.

Uses Notion's plain REST API directly via httpx — same style as
notifier.py's Telegram client — rather than the notion-client SDK, to keep
the dependency footprint the same as the rest of the project. Pinned to
API version 2022-06-28 (the stable "classic" database/page model) rather
than the newer database/data-source split introduced in 2025-09-03, since
the classic model is simpler and Notion keeps it working indefinitely via
the Notion-Version header — no reason to take on the extra complexity for
a personal tool with one flat database.

Self-provisioning: the target database is created once (as a subpage of
NOTION_PARENT_PAGE_ID) on first run and its id is cached in the shared
SQLite db's bot_state table (see storage.py) — same "CREATE IF NOT EXISTS"
spirit as storage.py's own schema. Per-job Notion page ids are cached the
same way, keyed by refnr, so re-running never creates duplicate rows.

Requires NOTION_API_KEY and NOTION_PARENT_PAGE_ID (see README "Sync to
Notion"). If either is unset, scout.py simply skips this step.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from analyzer import JobScore
from arbeitsagentur import Job
from storage import JobStorage

log = logging.getLogger("notion_sync")

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0
# Notion's rate limit is ~3 requests/second average; a small fixed pause
# between calls avoids tripping it during a run that syncs many new jobs
# at once, on top of the 429 retry/backoff below as a safety net.
MIN_REQUEST_INTERVAL_SECONDS = 0.35

DATABASE_ID_KEY = "notion_database_id"
DATABASE_TITLE = "Job Scout"

# Every newly-synced job enters the "Application Pipeline" board at this stage,
# so new hits show up in the intake column instead of an ungrouped "No Status"
# bucket. The option is created on the fly if the Status property doesn't have
# it yet (Notion auto-adds unknown select options on write).
DEFAULT_STATUS = "To Apply"

# Notion rich_text content is capped at 2000 chars per block.
_MAX_RICH_TEXT_CHARS = 1900


def _rich_text(text: str) -> list[dict]:
    text = (text or "")[:_MAX_RICH_TEXT_CHARS]
    if not text:
        return []
    return [{"type": "text", "text": {"content": text}}]


def _multi_select(values: list[str]) -> dict:
    # Notion auto-creates new multi_select options on write; no need to
    # pre-declare the full set of possible skills/flags in the schema.
    return {"multi_select": [{"name": v[:100]} for v in values if v]}


class NotionSyncError(Exception):
    """Raised on unrecoverable Notion API failures. Callers (scout.py)
    should catch this broadly and log a warning — a Notion outage must
    never take down the actual job search / scoring / Telegram alert."""


class NotionSync:
    def __init__(self, *, api_key: str, parent_page_id: str, storage: JobStorage,
                 timeout: httpx.Timeout = DEFAULT_TIMEOUT) -> None:
        if not api_key:
            raise ValueError("Notion API key is required.")
        if not parent_page_id:
            raise ValueError("Notion parent page id is required.")
        self.parent_page_id = parent_page_id
        self.storage = storage
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        self._last_request_at = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "NotionSync":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- HTTP with pacing + retry --------------------------------------------
    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> dict:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
            time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            self._last_request_at = time.monotonic()
            try:
                r = self._client.request(method, f"{API_BASE}{path}", json=json_body)
            except httpx.TransportError as exc:
                last_exc = exc
            else:
                if r.status_code == 429:
                    wait = float(r.headers.get("retry-after", RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))))
                    log.warning("Notion rate limited (attempt %d/%d) — waiting %.1fs", attempt, MAX_RETRIES, wait)
                    time.sleep(wait)
                    continue
                if r.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(f"Server error {r.status_code}", request=r.request, response=r)
                else:
                    if r.status_code >= 400:
                        raise NotionSyncError(f"Notion API {method} {path} failed: {r.status_code} {r.text[:300]}")
                    return r.json()

            if attempt < MAX_RETRIES:
                sleep_for = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                log.warning("Notion %s %s failed (attempt %d/%d): %s — retrying in %.1fs",
                            method, path, attempt, MAX_RETRIES, last_exc, sleep_for)
                time.sleep(sleep_for)

        raise NotionSyncError(f"Notion API {method} {path} failed after {MAX_RETRIES} attempts: {last_exc}")

    # -- database provisioning ------------------------------------------------
    def ensure_database(self) -> str:
        """Return the target database's id, creating it once if needed."""
        cached = self.storage.get_bot_state(DATABASE_ID_KEY)
        if cached:
            return cached

        log.info("No Notion database cached yet — creating %r under parent page.", DATABASE_TITLE)
        body = {
            "parent": {"type": "page_id", "page_id": self.parent_page_id},
            "title": [{"type": "text", "text": {"content": DATABASE_TITLE}}],
            "properties": {
                "Title": {"title": {}},
                "Employer": {"select": {}},
                "Location": {"rich_text": {}},
                "Score": {"number": {"format": "number"}},
                "Source": {"select": {}},
                "Key Skills": {"multi_select": {}},
                "Flags": {"multi_select": {}},
                "Posted Date": {"date": {}},
                "Seen At": {"date": {}},
                "URL": {"url": {}},
                "Refnr": {"rich_text": {}},
                "Status": {"select": {}},
            },
        }
        data = self._request("POST", "/databases", json_body=body)
        database_id = data["id"]
        self.storage.set_bot_state(DATABASE_ID_KEY, database_id)
        log.info("Created Notion database %s", database_id)
        return database_id

    # -- job sync ---------------------------------------------------------------
    def _page_cache_key(self, refnr: str) -> str:
        return f"notion_page:{refnr}"

    def sync_job(self, database_id: str, job: Job, score: JobScore | None) -> str | None:
        """Create a Notion page/row for one job, unless it's already synced.

        `job.extra["source"]` (set by scout.py's _collect_new_jobs, one of
        the JobSource registry names) becomes the "Source" property — jobs
        from a single run can come from different sources when queries.json
        mixes arbeitsagentur/greenhouse/lever/personio queries, so this is
        read per-job rather than passed once for the whole batch.

        Returns the page id, or None if the job was already synced (no-op)
        or the sync failed (logged, not raised — see sync_new_jobs)."""
        cache_key = self._page_cache_key(job.refnr)
        existing = self.storage.get_bot_state(cache_key)
        if existing:
            return None

        source = job.extra.get("source", "arbeitsagentur") if job.extra else "arbeitsagentur"
        properties: dict[str, Any] = {
            "Title": {"title": _rich_text(job.title) or [{"type": "text", "text": {"content": "(ohne Titel)"}}]},
            "Employer": {"select": {"name": (job.employer or "Unbekannt")[:100]}},
            "Location": {"rich_text": _rich_text(job.location)},
            "Source": {"select": {"name": source}},
            "Key Skills": _multi_select(score.key_skills if score else []),
            "Flags": _multi_select(score.flags if score else []),
            "Refnr": {"rich_text": _rich_text(job.refnr)},
            "Status": {"select": {"name": DEFAULT_STATUS}},
        }
        if score is not None:
            properties["Score"] = {"number": score.score}
        if job.posted_date:
            properties["Posted Date"] = {"date": {"start": job.posted_date[:10]}}
        properties["Seen At"] = {"date": {"start": datetime.now(timezone.utc).isoformat(timespec="seconds")}}
        if job.url:
            properties["URL"] = {"url": job.url}

        children = []
        if score and score.summary:
            children.append(_paragraph_block(score.summary))
        if job.description:
            children.append(_paragraph_block(job.description))

        body = {
            "parent": {"database_id": database_id},
            "properties": properties,
            "children": children,
        }
        data = self._request("POST", "/pages", json_body=body)
        page_id = data["id"]
        self.storage.set_bot_state(cache_key, page_id)
        return page_id

    def sync_new_jobs(self, jobs: list[tuple[Job, JobScore | None]]) -> int:
        """Sync every not-yet-synced job. Never raises — a Notion outage
        must not take down the rest of the run. Returns count synced."""
        if not jobs:
            return 0
        try:
            database_id = self.ensure_database()
        except (NotionSyncError, httpx.HTTPError) as exc:
            log.warning("Could not ensure Notion database — skipping sync this run: %s", exc)
            return 0

        synced = 0
        for job, score in jobs:
            try:
                page_id = self.sync_job(database_id, job, score)
            except (NotionSyncError, httpx.HTTPError):
                log.exception("Notion sync failed for job %s — skipping.", job.refnr)
                continue
            if page_id:
                synced += 1
        return synced


def _paragraph_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rich_text(text)},
    }
