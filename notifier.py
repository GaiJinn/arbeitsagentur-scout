"""
notifier — Telegram bot client.

Sends compact, formatted job summaries to a single chat. Splits long messages
to stay below Telegram's 4096-char limit.
"""
from __future__ import annotations

import html
import logging
from typing import Any

import httpx

from analyzer import JobScore
from arbeitsagentur import Job

log = logging.getLogger("notifier")

TELEGRAM_API = "https://api.telegram.org/bot{token}/"
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
        self._base_url = TELEGRAM_API.format(token=token)
        self._client = httpx.Client(timeout=15.0)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TelegramNotifier":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- low-level Telegram Bot API call ------------------------------------
    def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        files: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        url = self._base_url + method
        try:
            if files:
                r = self._client.post(url, data=payload, files=files, timeout=timeout)
            else:
                r = self._client.post(url, json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError:
            log.exception("Telegram API call failed: %s", method)
            return None

    def send_text(self, text: str) -> None:
        """Send one message to the configured chat."""
        self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    def send_cv_prompt(self, job: Job, score: JobScore) -> None:
        """Send a standalone message with a 'generate tailored CV' button."""
        text = (
            f"🎯 <b>[{score.score}/10]</b> {_escape(job.title)}\n"
            f"🏢 {_escape(job.employer)} · 📍 {_escape(job.location)}\n\n"
            "Lebenslauf für diese Stelle anpassen?"
        )
        keyboard = {
            "inline_keyboard": [[{"text": "📄 CV generieren", "callback_data": f"cv:{job.refnr}"}]]
        }
        self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        })

    def send_document(self, *, file_bytes: bytes, filename: str, caption: str = "") -> bool:
        files = {"document": (filename, file_bytes, "application/pdf")}
        payload: dict[str, Any] = {"chat_id": self.chat_id}
        if caption:
            payload["caption"] = caption
        result = self._call("sendDocument", payload, files=files, timeout=30.0)
        return result is not None

    def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    def remove_inline_keyboard(self, *, chat_id: int | str, message_id: int) -> None:
        self._call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        })

    def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict]:
        """Long-poll for new updates (callback queries, messages, ...)."""
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            r = self._client.get(self._base_url + "getUpdates", params=params, timeout=timeout + 10)
            r.raise_for_status()
            return r.json().get("result", [])
        except httpx.HTTPError:
            log.exception("getUpdates failed.")
            return []

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
            self.send_text(chunk)
