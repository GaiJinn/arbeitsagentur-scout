"""
notifier — Telegram bot client.

Sends compact, formatted job summaries to a single chat. Splits long messages
to stay below Telegram's 4096-char limit.
"""
from __future__ import annotations

import html
import logging

import httpx

from analyzer import JobScore
from arbeitsagentur import Job

log = logging.getLogger("notifier")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_CHARS = 3800  # below the 4096 hard limit, with margin


def _escape(text: str) -> str:
    """HTML-escape (we use parse_mode=HTML so links render as buttons)."""
    return html.escape(text or "", quote=False)


def _format_job(job: Job, score: JobScore | None) -> str:
    score_label = f"<b>[{score.score}/10]</b> " if score else ""
    title = _escape(job.title) or "(ohne Titel)"
    employer = _escape(job.employer) or "(unbekannt)"
    location = _escape(job.location) or ""
    posted = _escape(job.posted_date)
    summary = _escape(score.summary) if score else ""
    flags = (
        " ⚠️ " + ", ".join(_escape(f) for f in score.flags)
        if score and score.flags
        else ""
    )
    skills = (
        "\n<i>" + ", ".join(_escape(s) for s in score.key_skills) + "</i>"
        if score and score.key_skills
        else ""
    )
    url_line = f'\n<a href="{_escape(job.url)}">→ Inserat öffnen</a>' if job.url else ""

    return (
        f"{score_label}<b>{title}</b>\n"
        f"🏢 {employer} · 📍 {location} · 🗓 {posted}{flags}\n"
        f"{summary}"
        f"{skills}"
        f"{url_line}"
    )


class TelegramNotifier:
    def __init__(self, *, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self._url = TELEGRAM_API.format(token=token)
        self._client = httpx.Client(timeout=15.0)

    def _send(self, text: str) -> None:
        """Send one chunk."""
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = self._client.post(self._url, json=payload)
            r.raise_for_status()
        except httpx.HTTPError:
            log.exception("Telegram send failed.")

    def send_summary(
        self,
        jobs: list[tuple[Job, JobScore | None]],
        *,
        total_new: int,
    ) -> None:
        if not jobs:
            return

        header = (
            f"<b>🎯 arbeitsagentur-scout</b>\n"
            f"<i>{len(jobs)} relevante neue Jobs (von {total_new} insgesamt)</i>\n"
            "─────────────────────"
        )
        chunks = [header]
        current = ""
        for job, score in jobs:
            block = "\n\n" + _format_job(job, score)
            # Guard against a single job exceeding the per-message limit.
            if len(block) > MAX_MESSAGE_CHARS:
                block = block[: MAX_MESSAGE_CHARS - 1] + "…"
            if len(current) + len(block) > MAX_MESSAGE_CHARS:
                if current:
                    chunks.append(current)
                current = block.lstrip()
            else:
                current += block
        if current:
            chunks.append(current)

        for chunk in chunks:
            self._send(chunk)
