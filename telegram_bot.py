"""
telegram_bot — long-running listener for "generate tailored CV" buttons.

scout.py is a one-shot cron job and can't react to button clicks itself.
This is a separate, always-on process that long-polls Telegram for
callback_query updates of the form "cv:<refnr>", looks the job up in the
shared SQLite db (populated by scout.py), tailors the candidate's base CV
(a PDF) to that job via an LLM, and sends the result back as a new PDF.

Usage:
    python telegram_bot.py
"""
from __future__ import annotations

import contextvars
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

from cv_generator import generate_tailored_cv_pdf
from notifier import TelegramNotifier
from storage import JobStorage

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DEFAULT_DB_PATH = Path(__file__).parent / "data" / "jobs.db"
DB_PATH = Path(os.getenv("DB_PATH") or DEFAULT_DB_PATH)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BASE_CV_PATH = Path(os.getenv("BASE_CV_PATH") or Path(__file__).parent / "cv.pdf")

# Touched once per poll cycle (~every 30s, see run()). Docker's HEALTHCHECK
# (see docker-compose.yml) considers the process dead if this file hasn't
# been updated recently — this is a long-polling loop with no HTTP endpoint
# of its own, so a heartbeat file is the simplest way to notice it silently
# hung or crashed inside a container.
HEARTBEAT_PATH = Path(os.getenv("HEARTBEAT_PATH") or "/tmp/telegram_bot_heartbeat")

OFFSET_KEY = "telegram_update_offset"
CALLBACK_PREFIX = "cv:"

# This process is long-running (not a one-shot cron run like scout.py), so a
# single "run id" per process wouldn't help trace anything — instead, each
# handled callback_query (one button tap = one CV generation request) gets
# its own short request id, threaded through logs via a contextvar so nested
# calls (cv_generator, notifier, ...) pick it up without needing it passed
# down explicitly.
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        return True


logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] [req=%(request_id)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RequestIdFilter())
# httpx logs every request at INFO — and this bot long-polls getUpdates every
# ~30s with the Telegram token in the URL path, so at INFO it would write the
# token in plaintext to the log on every poll. Quiet httpx to WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("telegram_bot")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def handle_callback_query(
    cq: dict,
    *,
    storage: JobStorage,
    notifier: TelegramNotifier,
    base_cv_path: Path,
    api_key: str,
) -> None:
    data = cq.get("data", "")
    if not data.startswith(CALLBACK_PREFIX):
        return
    refnr = data[len(CALLBACK_PREFIX):]
    message = cq.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    notifier.answer_callback_query(cq["id"], text="Generiere CV…")
    if chat_id is not None and message_id is not None:
        notifier.remove_inline_keyboard(chat_id=chat_id, message_id=message_id)

    job = storage.get_job(refnr)
    if job is None:
        log.warning("CV requested for unknown refnr=%s", refnr)
        notifier.send_text(f"⚠️ Stelle {refnr} nicht mehr in der Datenbank.")
        return

    if not base_cv_path.exists():
        log.error("BASE_CV_PATH not found: %s", base_cv_path)
        notifier.send_text(f"⚠️ Keine Basis-CV gefunden unter {base_cv_path}.")
        return

    try:
        pdf_bytes = generate_tailored_cv_pdf(
            base_cv_path=base_cv_path,
            job_title=job["title"],
            job_employer=job["employer"],
            job_location=job["location"],
            job_description=job["description"] or "",
            api_key=api_key,
        )
    except Exception:
        log.exception("CV generation failed for refnr=%s", refnr)
        notifier.send_text(f"⚠️ CV-Generierung fehlgeschlagen für {job['title']}.")
        return

    safe_employer = (job["employer"] or "Stelle").replace(" ", "_")
    notifier.send_document(
        file_bytes=pdf_bytes,
        filename=f"CV_{safe_employer}.pdf",
        caption=f"📄 Angepasste CV für: {job['title']} @ {job['employer']}",
    )
    log.info("Sent tailored CV for refnr=%s", refnr)


def _touch_heartbeat() -> None:
    """Record that the poll loop is still alive. Best-effort: a failure to
    write the heartbeat shouldn't take the bot down, just fail the container
    healthcheck (which is the point — someone should notice)."""
    try:
        HEARTBEAT_PATH.write_text(str(int(time.time())))
    except OSError:
        log.warning("Could not write heartbeat file %s", HEARTBEAT_PATH)


def run() -> int:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        sys.exit("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID required to run telegram_bot.py.")
    if not GROQ_API_KEY:
        sys.exit("GROQ_API_KEY required to run telegram_bot.py.")

    storage = JobStorage(DB_PATH)
    notifier = TelegramNotifier(token=TELEGRAM_TOKEN, chat_id=TELEGRAM_CHAT_ID)

    offset_str = storage.get_bot_state(OFFSET_KEY)
    offset = int(offset_str) + 1 if offset_str else None

    log.info("telegram_bot listening (base_cv=%s)...", BASE_CV_PATH)
    _touch_heartbeat()
    try:
        while True:
            updates = notifier.get_updates(offset=offset, timeout=30)
            _touch_heartbeat()
            for update in updates:
                offset = update["update_id"] + 1
                storage.set_bot_state(OFFSET_KEY, str(offset))
                cq = update.get("callback_query")
                if not cq:
                    continue
                token = _request_id_var.set(uuid.uuid4().hex[:8])
                try:
                    handle_callback_query(
                        cq,
                        storage=storage,
                        notifier=notifier,
                        base_cv_path=BASE_CV_PATH,
                        api_key=GROQ_API_KEY,
                    )
                except Exception:
                    log.exception("Error handling callback_query: %s", cq.get("id"))
                finally:
                    _request_id_var.reset(token)
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        notifier.close()
    return 0


if __name__ == "__main__":
    sys.exit(run())
