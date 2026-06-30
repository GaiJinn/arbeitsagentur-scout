import httpx
import pytest
import respx

from arbeitsagentur import ArbeitsagenturClient, Job, API_BASE


# -- Job.from_api -------------------------------------------------------------

def test_from_api_combines_plz_and_ort():
    job = Job.from_api({
        "refnr": "abc",
        "titel": "Werkstudent Software",
        "arbeitgeber": "Beispiel GmbH",
        "arbeitsort": {"plz": "40474", "ort": "Düsseldorf"},
        "aktuelleVeroeffentlichungsdatum": "2026-06-01",
        "beruf": "Informatiker",
    })
    assert job.location == "40474 Düsseldorf"
    assert job.title == "Werkstudent Software"


def test_from_api_prefers_external_url():
    job = Job.from_api({"refnr": "abc", "externeUrl": "https://employer.example/job"})
    assert job.url == "https://employer.example/job"


def test_from_api_falls_back_to_agentur_url_without_external():
    job = Job.from_api({"refnr": "abc123"})
    assert job.url == "https://www.arbeitsagentur.de/jobsuche/jobdetail/abc123"


def test_from_api_handles_missing_arbeitsort():
    job = Job.from_api({"refnr": "abc"})
    assert job.location == ""


# -- pagination -----------------------------------------------------------

def _page_response(n: int) -> dict:
    return {"stellenangebote": [{"refnr": f"job-{i}"} for i in range(n)]}


@respx.mock
def test_search_stops_on_short_page():
    route = respx.get(f"{API_BASE}/jobs").mock(
        side_effect=[
            httpx.Response(200, json=_page_response(50)),
            httpx.Response(200, json=_page_response(20)),
        ]
    )
    with ArbeitsagenturClient() as client:
        jobs = client.search(was="Python", wo="Berlin", size=50)

    assert route.call_count == 2
    assert len(jobs) == 70


@respx.mock
def test_search_respects_max_pages_cap():
    respx.get(f"{API_BASE}/jobs").mock(return_value=httpx.Response(200, json=_page_response(50)))
    with ArbeitsagenturClient() as client:
        jobs = client.search(was="Python", wo="Berlin", size=50, max_pages=3)

    assert len(jobs) == 150


@respx.mock
def test_search_single_short_page_no_extra_request():
    route = respx.get(f"{API_BASE}/jobs").mock(return_value=httpx.Response(200, json=_page_response(5)))
    with ArbeitsagenturClient() as client:
        jobs = client.search(was="Python", wo="Berlin", size=50)

    assert route.call_count == 1
    assert len(jobs) == 5


# -- retry ------------------------------------------------------------------

@respx.mock
def test_search_retries_on_server_error(monkeypatch):
    monkeypatch.setattr("arbeitsagentur.time.sleep", lambda _: None)
    route = respx.get(f"{API_BASE}/jobs").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=_page_response(1)),
        ]
    )
    with ArbeitsagenturClient() as client:
        jobs = client.search(was="Python", wo="Berlin", size=50)

    assert route.call_count == 2
    assert len(jobs) == 1


@respx.mock
def test_search_does_not_retry_on_client_error(monkeypatch):
    monkeypatch.setattr("arbeitsagentur.time.sleep", lambda _: None)
    route = respx.get(f"{API_BASE}/jobs").mock(return_value=httpx.Response(400))
    with ArbeitsagenturClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.search(was="Python", wo="Berlin", size=50)

    assert route.call_count == 1


@respx.mock
def test_search_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr("arbeitsagentur.time.sleep", lambda _: None)
    route = respx.get(f"{API_BASE}/jobs").mock(return_value=httpx.Response(500))
    with ArbeitsagenturClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.search(was="Python", wo="Berlin", size=50)

    assert route.call_count == 3


# -- fetch_details ------------------------------------------------------------

@respx.mock
def test_fetch_details_returns_empty_string_on_persistent_failure(monkeypatch):
    monkeypatch.setattr("arbeitsagentur.time.sleep", lambda _: None)
    respx.get(url__regex=rf"{API_BASE}/jobdetails/.*").mock(return_value=httpx.Response(500))
    with ArbeitsagenturClient() as client:
        text = client.fetch_details("some-refnr")

    assert text == ""


def test_fetch_details_returns_empty_string_for_blank_refnr():
    with ArbeitsagenturClient() as client:
        assert client.fetch_details("") == ""
