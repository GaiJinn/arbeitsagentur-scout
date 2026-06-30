from unittest.mock import MagicMock

import pytest

import telegram_bot


def make_callback_query(data: str, *, cq_id: str = "cq-1", chat_id: int = 123, message_id: int = 456) -> dict:
    return {
        "id": cq_id,
        "data": data,
        "message": {"chat": {"id": chat_id}, "message_id": message_id},
    }


def test_handle_callback_query_ignores_non_cv_callbacks():
    storage = MagicMock()
    notifier = MagicMock()
    telegram_bot.handle_callback_query(
        make_callback_query("other:thing"),
        storage=storage,
        notifier=notifier,
        base_cv_path=None,
        api_key="key",
    )
    notifier.answer_callback_query.assert_not_called()
    storage.get_job.assert_not_called()


def test_handle_callback_query_removes_keyboard_and_answers(tmp_path):
    storage = MagicMock()
    storage.get_job.return_value = None
    notifier = MagicMock()

    telegram_bot.handle_callback_query(
        make_callback_query("cv:ref-1"),
        storage=storage,
        notifier=notifier,
        base_cv_path=tmp_path / "cv.pdf",
        api_key="key",
    )

    notifier.answer_callback_query.assert_called_once()
    notifier.remove_inline_keyboard.assert_called_once_with(chat_id=123, message_id=456)


def test_handle_callback_query_unknown_job_sends_warning(tmp_path):
    storage = MagicMock()
    storage.get_job.return_value = None
    notifier = MagicMock()

    telegram_bot.handle_callback_query(
        make_callback_query("cv:missing-refnr"),
        storage=storage,
        notifier=notifier,
        base_cv_path=tmp_path / "cv.pdf",
        api_key="key",
    )

    notifier.send_text.assert_called_once()
    assert "missing-refnr" in notifier.send_text.call_args[0][0]
    notifier.send_document.assert_not_called()


def test_handle_callback_query_missing_base_cv_sends_warning(tmp_path):
    storage = MagicMock()
    storage.get_job.return_value = {
        "refnr": "ref-1", "title": "Werkstudent", "employer": "Beispiel GmbH",
        "location": "Berlin", "description": "...",
    }
    notifier = MagicMock()
    missing_cv_path = tmp_path / "does-not-exist.pdf"

    telegram_bot.handle_callback_query(
        make_callback_query("cv:ref-1"),
        storage=storage,
        notifier=notifier,
        base_cv_path=missing_cv_path,
        api_key="key",
    )

    notifier.send_text.assert_called_once()
    notifier.send_document.assert_not_called()


def test_handle_callback_query_generates_and_sends_pdf(tmp_path, monkeypatch):
    base_cv_path = tmp_path / "cv.pdf"
    base_cv_path.write_bytes(b"%PDF-fake")

    storage = MagicMock()
    storage.get_job.return_value = {
        "refnr": "ref-1", "title": "Werkstudent KI", "employer": "Beispiel GmbH",
        "location": "Berlin", "description": "KI-Projekte",
    }
    notifier = MagicMock()
    monkeypatch.setattr(
        telegram_bot, "generate_tailored_cv_pdf", lambda **_: b"%PDF-generated"
    )

    telegram_bot.handle_callback_query(
        make_callback_query("cv:ref-1"),
        storage=storage,
        notifier=notifier,
        base_cv_path=base_cv_path,
        api_key="key",
    )

    notifier.send_document.assert_called_once()
    kwargs = notifier.send_document.call_args.kwargs
    assert kwargs["file_bytes"] == b"%PDF-generated"
    assert "Beispiel_GmbH" in kwargs["filename"]


def test_handle_callback_query_generation_failure_sends_warning(tmp_path, monkeypatch):
    base_cv_path = tmp_path / "cv.pdf"
    base_cv_path.write_bytes(b"%PDF-fake")

    storage = MagicMock()
    storage.get_job.return_value = {
        "refnr": "ref-1", "title": "Werkstudent KI", "employer": "Beispiel GmbH",
        "location": "Berlin", "description": "KI-Projekte",
    }
    notifier = MagicMock()

    def boom(**_):
        raise ValueError("LLM exploded")

    monkeypatch.setattr(telegram_bot, "generate_tailored_cv_pdf", boom)

    telegram_bot.handle_callback_query(
        make_callback_query("cv:ref-1"),
        storage=storage,
        notifier=notifier,
        base_cv_path=base_cv_path,
        api_key="key",
    )

    notifier.send_text.assert_called_once()
    notifier.send_document.assert_not_called()
