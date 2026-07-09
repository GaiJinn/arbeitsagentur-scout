import json

import httpx
import respx

from analyzer import JobScore
from arbeitsagentur import Job
from notifier import TelegramNotifier

TOKEN = "test-token"
API_BASE = f"https://api.telegram.org/bot{TOKEN}"


def test_escape_neutralizes_quotes_in_href():
    # Job URLs land inside an href="..." attribute. An unescaped `"` would
    # terminate the attribute early and truncate the link; `<`/`>` could
    # open rogue tags. _escape(quote=True) must neutralize all of them.
    from notifier import _escape, _format_job

    assert _escape('a"b') == "a&quot;b"
    assert _escape("a'b") == "a&#x27;b"

    job = Job(
        refnr="ref-q",
        title='Dev "Backend" <m/w/d>',
        employer="Beispiel GmbH",
        location="Düsseldorf",
        posted_date="2026-06-01",
        url='https://example.com/job?q="x"',
    )
    text = _format_job(job, JobScore(score=8, summary="ok"))
    assert '"https://example.com/job?q=&quot;x&quot;"' in text
    assert 'q="x"' not in text  # raw quote must never reach the attribute
    assert "&lt;m/w/d&gt;" in text


def make_job(refnr: str = "ref-1") -> Job:
    return Job(
        refnr=refnr,
        title="Werkstudent KI",
        employer="Beispiel GmbH",
        location="Düsseldorf",
        posted_date="2026-06-01",
        url="https://example.com/job",
    )


@respx.mock
def test_send_cv_prompt_includes_callback_data_with_refnr():
    route = respx.post(f"{API_BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    notifier = TelegramNotifier(token=TOKEN, chat_id="123")
    notifier.send_cv_prompt(make_job("ref-42"), JobScore(score=8, summary="ok"))

    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["chat_id"] == "123"
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "cv:ref-42"


@respx.mock
def test_send_text_returns_true_on_success():
    respx.post(f"{API_BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    notifier = TelegramNotifier(token=TOKEN, chat_id="123")
    assert notifier.send_text("hi") is True


@respx.mock
def test_send_text_returns_false_on_error():
    # A Telegram 400 (e.g. bad chat_id) must surface as False, not a swallowed
    # success — this is what lets scout.py avoid logging a false "alert sent".
    respx.post(f"{API_BASE}/sendMessage").mock(return_value=httpx.Response(400))
    notifier = TelegramNotifier(token=TOKEN, chat_id="123")
    assert notifier.send_text("hi") is False


@respx.mock
def test_send_summary_returns_true_when_all_chunks_succeed():
    respx.post(f"{API_BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    notifier = TelegramNotifier(token=TOKEN, chat_id="123")
    ok = notifier.send_summary([(make_job(), JobScore(score=8, summary="ok"))], total_new=1)
    assert ok is True


@respx.mock
def test_send_summary_returns_false_when_send_fails():
    respx.post(f"{API_BASE}/sendMessage").mock(return_value=httpx.Response(400))
    notifier = TelegramNotifier(token=TOKEN, chat_id="123")
    ok = notifier.send_summary([(make_job(), JobScore(score=8, summary="ok"))], total_new=1)
    assert ok is False


@respx.mock
def test_send_document_posts_multipart():
    route = respx.post(f"{API_BASE}/sendDocument").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    notifier = TelegramNotifier(token=TOKEN, chat_id="123")
    ok = notifier.send_document(file_bytes=b"%PDF-fake", filename="cv.pdf", caption="hi")

    assert ok is True
    assert route.called
    assert b"cv.pdf" in route.calls[0].request.content


@respx.mock
def test_send_document_returns_false_on_failure():
    respx.post(f"{API_BASE}/sendDocument").mock(return_value=httpx.Response(500))
    notifier = TelegramNotifier(token=TOKEN, chat_id="123")
    ok = notifier.send_document(file_bytes=b"data", filename="cv.pdf")
    assert ok is False


@respx.mock
def test_get_updates_returns_result_list():
    respx.get(f"{API_BASE}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": [{"update_id": 1}]})
    )
    notifier = TelegramNotifier(token=TOKEN, chat_id="123")
    updates = notifier.get_updates(offset=1)
    assert updates == [{"update_id": 1}]


@respx.mock
def test_get_updates_returns_empty_list_on_error():
    respx.get(f"{API_BASE}/getUpdates").mock(return_value=httpx.Response(500))
    notifier = TelegramNotifier(token=TOKEN, chat_id="123")
    assert notifier.get_updates() == []


@respx.mock
def test_answer_callback_query_posts_to_correct_endpoint():
    route = respx.post(f"{API_BASE}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    notifier = TelegramNotifier(token=TOKEN, chat_id="123")
    notifier.answer_callback_query("cq-1", text="hi")
    assert route.called


@respx.mock
def test_remove_inline_keyboard_posts_empty_keyboard():
    route = respx.post(f"{API_BASE}/editMessageReplyMarkup").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    notifier = TelegramNotifier(token=TOKEN, chat_id="123")
    notifier.remove_inline_keyboard(chat_id=123, message_id=456)
    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["reply_markup"]["inline_keyboard"] == []
