"""
Integration test: drives scout.main() end-to-end with every external HTTP
call mocked via respx (arbeitsagentur search + jobdetails, Groq chat
completions, Telegram sendMessage). No unit-level mocking of our own
modules — this is meant to catch wiring mistakes the per-module unit tests
can't (e.g. a signature mismatch between _collect_new_jobs and main(),
or the JobSource registry not actually reaching the real ArbeitsagenturClient).

No real network calls are made or API keys needed.
"""
from __future__ import annotations

import base64
import json

import httpx
import respx

import analyzer
import scout
from arbeitsagentur import API_BASE
from storage import JobStorage

TELEGRAM_BASE = "https://api.telegram.org/botfake-telegram-token"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


def _configure(monkeypatch, tmp_path, *, queries):
    profile_path = tmp_path / "profile.md"
    profile_path.write_text("Informatik-Student, Python, Linux, n8n.", encoding="utf-8")
    monkeypatch.setattr(analyzer, "PROFILE_PATH", profile_path)

    db_path = tmp_path / "jobs.db"
    monkeypatch.setattr(scout, "DB_PATH", db_path)
    monkeypatch.setattr(scout, "SEARCH_QUERIES", queries)
    monkeypatch.setattr(scout, "GROQ_API_KEY", "fake-groq-key")
    monkeypatch.setattr(scout, "TELEGRAM_TOKEN", "fake-telegram-token")
    monkeypatch.setattr(scout, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(scout, "SCORE_THRESHOLD", 6)
    # No cv.pdf in the test sandbox, so BASE_CV_PATH.exists() is False and no
    # CV-generation prompt path is exercised here (covered by
    # test_telegram_bot.py / test_cv_generator.py instead).
    return db_path


@respx.mock
def test_main_end_to_end_scores_job_and_sends_telegram_alert(monkeypatch, tmp_path):
    db_path = _configure(monkeypatch, tmp_path, queries=[
        {"label": "Werkstudent KI Berlin", "params": {"was": "KI", "wo": "Berlin", "veroeffentlichtseit": 7}},
    ])

    search_route = respx.get(f"{API_BASE}/jobs").mock(
        return_value=httpx.Response(200, json={
            "stellenangebote": [{
                "refnr": "abc-123",
                "titel": "Werkstudent KI & Automatisierung",
                "arbeitgeber": "Beispiel GmbH",
                "arbeitsort": {"plz": "10115", "ort": "Berlin"},
                "aktuelleVeroeffentlichungsdatum": "2026-06-01",
                "beruf": "Informatiker",
            }],
        })
    )
    encoded_refnr = base64.b64encode(b"abc-123").decode()
    details_route = respx.get(f"{API_BASE}/jobdetails/{encoded_refnr}").mock(
        return_value=httpx.Response(200, json={
            "stellenbeschreibung": "Wir suchen einen Werkstudenten für KI-Automatisierung mit Python und n8n.",
            "titel": "Werkstudent KI & Automatisierung",
            "beruf": "Informatiker",
        })
    )
    groq_route = respx.post(GROQ_CHAT_URL).mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "score": 9,
                "summary": "Perfekter Fit: Python, n8n, KI explizit gefordert.",
                "key_skills": ["Python", "n8n"],
                "fit_reasons": ["Skill-Match"],
                "flags": [],
            })}}]
        })
    )
    telegram_route = respx.post(f"{TELEGRAM_BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )

    exit_code = scout.main()

    assert exit_code == 0
    assert search_route.call_count == 1
    assert details_route.call_count == 1
    assert groq_route.call_count == 1
    assert telegram_route.call_count >= 1

    storage = JobStorage(db_path)
    saved = storage.get_job("abc-123")
    assert saved is not None
    assert saved["score"] == 9
    assert saved["employer"] == "Beispiel GmbH"
    assert json.loads(saved["key_skills"]) == ["Python", "n8n"]

    # send_summary splits into a header chunk + a jobs chunk (see notifier.py)
    # — check across every sendMessage call, not just the first.
    all_sent_text = " ".join(
        call.request.content.decode("utf-8") for call in telegram_route.calls
    )
    assert "Werkstudent KI" in all_sent_text


@respx.mock
def test_main_second_run_does_not_rescan_already_seen_job(monkeypatch, tmp_path):
    """The whole point of the SQLite dedup layer: a refnr seen in run 1
    should not trigger a second LLM call / Telegram alert in run 2."""
    db_path = _configure(monkeypatch, tmp_path, queries=[
        {"label": "Werkstudent KI Berlin", "params": {"was": "KI", "wo": "Berlin"}},
    ])

    job_payload = {
        "stellenangebote": [{
            "refnr": "dup-1",
            "titel": "Werkstudent Software",
            "arbeitgeber": "Firma X",
            "arbeitsort": {"ort": "Köln"},
            "aktuelleVeroeffentlichungsdatum": "2026-06-01",
            "beruf": "Informatiker",
        }],
    }
    respx.get(f"{API_BASE}/jobs").mock(return_value=httpx.Response(200, json=job_payload))
    encoded_refnr = base64.b64encode(b"dup-1").decode()
    respx.get(f"{API_BASE}/jobdetails/{encoded_refnr}").mock(
        return_value=httpx.Response(200, json={"stellenbeschreibung": "Volltext hier."})
    )
    groq_route = respx.post(GROQ_CHAT_URL).mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({"score": 8, "summary": "ok"})}}]
        })
    )
    telegram_route = respx.post(f"{TELEGRAM_BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )

    assert scout.main() == 0
    assert groq_route.call_count == 1
    assert telegram_route.call_count >= 1

    telegram_route.reset()
    groq_route.reset()

    assert scout.main() == 0
    assert groq_route.call_count == 0  # job already in db — never re-scored
    assert telegram_route.call_count == 0  # nothing new — no alert sent
