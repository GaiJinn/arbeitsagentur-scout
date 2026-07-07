"""
Tests for notion_sync.py — all Notion API calls mocked via respx, no real
network calls or tokens needed.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from analyzer import JobScore
from arbeitsagentur import Job
from notion_sync import API_BASE, NotionSync, NotionSyncError


def make_job(refnr="job-1", **overrides) -> Job:
    defaults = dict(
        refnr=refnr,
        title="Werkstudent KI",
        employer="Beispiel GmbH",
        location="Düsseldorf",
        posted_date="2026-06-01",
        profession="Informatiker",
        url="https://example.com/job",
        description="Vollständige Stellenbeschreibung.",
    )
    defaults.update(overrides)
    return Job(**defaults)


class FakeStorage:
    """Just enough of JobStorage's bot_state interface for NotionSync."""

    def __init__(self):
        self.state: dict[str, str] = {}

    def get_bot_state(self, key, default=None):
        return self.state.get(key, default)

    def set_bot_state(self, key, value):
        self.state[key] = value


@pytest.fixture
def storage():
    return FakeStorage()


@pytest.fixture
def sync(storage, monkeypatch):
    # The inter-request pacing sleep (MIN_REQUEST_INTERVAL_SECONDS) exists to
    # be polite to the real Notion API in production; it serves no purpose
    # against a respx-mocked transport and would just slow the suite down.
    monkeypatch.setattr("notion_sync.time.sleep", lambda _: None)
    with NotionSync(api_key="fake-token", parent_page_id="parent-123", storage=storage) as s:
        yield s


# -- construction -----------------------------------------------------------

def test_requires_api_key(storage):
    with pytest.raises(ValueError):
        NotionSync(api_key="", parent_page_id="parent-123", storage=storage)


def test_requires_parent_page_id(storage):
    with pytest.raises(ValueError):
        NotionSync(api_key="fake-token", parent_page_id="", storage=storage)


# -- ensure_database ----------------------------------------------------------

@respx.mock
def test_ensure_database_creates_once_and_caches(sync, storage):
    route = respx.post(f"{API_BASE}/databases").mock(
        return_value=httpx.Response(200, json={"id": "db-abc"})
    )
    database_id = sync.ensure_database()
    assert database_id == "db-abc"
    assert route.call_count == 1
    assert storage.get_bot_state("notion_database_id") == "db-abc"

    # Second call must not hit the API again — served from cache.
    database_id_2 = sync.ensure_database()
    assert database_id_2 == "db-abc"
    assert route.call_count == 1


@respx.mock
def test_ensure_database_sends_expected_schema(sync):
    route = respx.post(f"{API_BASE}/databases").mock(
        return_value=httpx.Response(200, json={"id": "db-abc"})
    )
    sync.ensure_database()

    body = route.calls[0].request.content
    import json as _json
    payload = _json.loads(body)
    assert payload["parent"] == {"type": "page_id", "page_id": "parent-123"}
    for prop in ("Title", "Employer", "Location", "Score", "Source",
                 "Key Skills", "Flags", "Posted Date", "Seen At", "URL", "Refnr"):
        assert prop in payload["properties"]


# -- sync_job -----------------------------------------------------------------

@respx.mock
def test_sync_job_creates_page_and_caches_id(sync, storage):
    route = respx.post(f"{API_BASE}/pages").mock(
        return_value=httpx.Response(200, json={"id": "page-1"})
    )
    job = make_job()
    job.extra["source"] = "greenhouse"
    score = JobScore(score=9, summary="Guter Fit", key_skills=["Python"], flags=[])

    page_id = sync.sync_job("db-abc", job, score)

    assert page_id == "page-1"
    assert route.call_count == 1
    assert storage.get_bot_state("notion_page:job-1") == "page-1"

    import json as _json
    payload = _json.loads(route.calls[0].request.content)
    assert payload["parent"] == {"database_id": "db-abc"}
    props = payload["properties"]
    assert props["Title"]["title"][0]["text"]["content"] == "Werkstudent KI"
    assert props["Employer"]["select"]["name"] == "Beispiel GmbH"
    assert props["Score"]["number"] == 9
    assert props["Source"]["select"]["name"] == "greenhouse"
    assert props["Key Skills"]["multi_select"] == [{"name": "Python"}]
    assert props["URL"]["url"] == "https://example.com/job"
    assert props["Posted Date"]["date"]["start"] == "2026-06-01"


@respx.mock
def test_sync_job_skips_already_synced(sync, storage):
    storage.set_bot_state("notion_page:job-1", "page-existing")
    route = respx.post(f"{API_BASE}/pages").mock(return_value=httpx.Response(200, json={"id": "page-new"}))

    result = sync.sync_job("db-abc", make_job(), JobScore(score=8, summary="ok"))

    assert result is None
    assert route.call_count == 0  # never called — cache hit


@respx.mock
def test_sync_job_handles_unscored_job(sync):
    route = respx.post(f"{API_BASE}/pages").mock(return_value=httpx.Response(200, json={"id": "page-1"}))
    page_id = sync.sync_job("db-abc", make_job(), None)
    assert page_id == "page-1"

    import json as _json
    payload = _json.loads(route.calls[0].request.content)
    assert "Score" not in payload["properties"]  # omitted, not sent as null/0
    assert payload["properties"]["Key Skills"]["multi_select"] == []


@respx.mock
def test_sync_job_defaults_source_when_extra_empty(sync):
    route = respx.post(f"{API_BASE}/pages").mock(return_value=httpx.Response(200, json={"id": "page-1"}))
    sync.sync_job("db-abc", make_job(), None)  # job.extra untouched, no "source" key

    import json as _json
    payload = _json.loads(route.calls[0].request.content)
    assert payload["properties"]["Source"]["select"]["name"] == "arbeitsagentur"


@respx.mock
def test_sync_job_omits_empty_posted_date_and_url(sync):
    route = respx.post(f"{API_BASE}/pages").mock(return_value=httpx.Response(200, json={"id": "page-1"}))
    job = make_job(posted_date="", url="")
    sync.sync_job("db-abc", job, None)

    import json as _json
    payload = _json.loads(route.calls[0].request.content)
    assert "Posted Date" not in payload["properties"]
    assert "URL" not in payload["properties"]


# -- error handling / retries --------------------------------------------------

@respx.mock
def test_request_raises_notion_sync_error_on_4xx(sync):
    respx.post(f"{API_BASE}/pages").mock(return_value=httpx.Response(400, json={"message": "bad request"}))
    with pytest.raises(NotionSyncError):
        sync.sync_job("db-abc", make_job(), None)


@respx.mock
def test_request_retries_on_429_honoring_retry_after(sync, monkeypatch):
    monkeypatch.setattr("notion_sync.time.sleep", lambda _: None)
    route = respx.post(f"{API_BASE}/pages").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "1"}, json={"message": "rate limited"}),
            httpx.Response(200, json={"id": "page-1"}),
        ]
    )
    page_id = sync.sync_job("db-abc", make_job(), None)
    assert page_id == "page-1"
    assert route.call_count == 2


@respx.mock
def test_request_retries_on_server_error_then_gives_up(sync, monkeypatch):
    monkeypatch.setattr("notion_sync.time.sleep", lambda _: None)
    route = respx.post(f"{API_BASE}/pages").mock(return_value=httpx.Response(500))
    with pytest.raises(NotionSyncError):
        sync.sync_job("db-abc", make_job(), None)
    assert route.call_count == 3  # MAX_RETRIES


# -- sync_new_jobs (the entry point scout.py calls) ----------------------------

@respx.mock
def test_sync_new_jobs_returns_zero_for_empty_list(sync):
    assert sync.sync_new_jobs([]) == 0


@respx.mock
def test_sync_new_jobs_syncs_all_and_skips_cached(sync, storage):
    respx.post(f"{API_BASE}/databases").mock(return_value=httpx.Response(200, json={"id": "db-abc"}))
    respx.post(f"{API_BASE}/pages").mock(return_value=httpx.Response(200, json={"id": "page-x"}))

    storage.set_bot_state("notion_page:already-synced", "page-old")
    jobs = [
        (make_job("job-a"), JobScore(score=7, summary="ok")),
        (make_job("already-synced"), JobScore(score=5, summary="ok")),
        (make_job("job-b"), None),
    ]

    synced = sync.sync_new_jobs(jobs)
    assert synced == 2  # job-a and job-b; already-synced skipped


@respx.mock
def test_sync_new_jobs_continues_after_one_job_fails(sync):
    respx.post(f"{API_BASE}/databases").mock(return_value=httpx.Response(200, json={"id": "db-abc"}))
    respx.post(f"{API_BASE}/pages").mock(
        side_effect=[
            httpx.Response(400, json={"message": "bad"}),
            httpx.Response(200, json={"id": "page-ok"}),
        ]
    )
    jobs = [(make_job("job-bad"), None), (make_job("job-ok"), None)]

    synced = sync.sync_new_jobs(jobs)
    assert synced == 1  # job-bad failed and was skipped, job-ok succeeded


@respx.mock
def test_sync_new_jobs_returns_zero_when_database_creation_fails(sync):
    respx.post(f"{API_BASE}/databases").mock(return_value=httpx.Response(404, json={"message": "not found"}))
    synced = sync.sync_new_jobs([(make_job(), None)])
    assert synced == 0  # logged and swallowed, never raised
