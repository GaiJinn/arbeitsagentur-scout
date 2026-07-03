"""
Tests for ats_sources.py's GreenhouseSource / LeverSource / PersonioSource —
all HTTP mocked via respx, no real network calls.
"""
from __future__ import annotations

import httpx
import respx

from ats_sources import GreenhouseSource, LeverSource, PersonioSource, _matches_keywords, _strip_html


# -- helpers --------------------------------------------------------------------

def test_strip_html_removes_tags_and_unescapes_entities():
    assert _strip_html("<p>Hello &amp; welcome</p>") == "Hello & welcome"


def test_strip_html_handles_empty_input():
    assert _strip_html("") == ""
    assert _strip_html(None) == ""


def test_matches_keywords_requires_all_words_present():
    assert _matches_keywords("Werkstudent KI Automatisierung", "KI Werkstudent")
    assert not _matches_keywords("Werkstudent KI", "Praktikum")


def test_matches_keywords_empty_string_matches_everything():
    assert _matches_keywords("anything", "")


# -- GreenhouseSource -----------------------------------------------------------

GREENHOUSE_LIST_RESPONSE = {
    "jobs": [
        {
            "id": 127817,
            "title": "Werkstudent KI & Automatisierung",
            "updated_at": "2026-06-01T10:00:00-05:00",
            "location": {"name": "Berlin"},
            "absolute_url": "https://boards.greenhouse.io/firmax/jobs/127817",
            "content": "<p>Wir suchen einen <b>Werkstudenten</b> für KI-Projekte.</p>",
        },
        {
            "id": 127818,
            "title": "Senior Sales Manager",
            "updated_at": "2026-06-02T10:00:00-05:00",
            "location": {"name": "Hamburg"},
            "absolute_url": "https://boards.greenhouse.io/firmax/jobs/127818",
            "content": "<p>Sales role, no tech skills required.</p>",
        },
    ],
}


@respx.mock
def test_greenhouse_search_filters_by_keywords():
    respx.get("https://boards-api.greenhouse.io/v1/boards/firmax/jobs").mock(
        return_value=httpx.Response(200, json=GREENHOUSE_LIST_RESPONSE)
    )
    with GreenhouseSource() as source:
        jobs = source.search(board_token="firmax", employer="Firma X GmbH", keywords="KI")

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Werkstudent KI & Automatisierung"
    assert job.employer == "Firma X GmbH"
    assert job.location == "Berlin"
    assert job.refnr == "greenhouse:firmax:127817"
    assert "Werkstudenten" in job.description
    assert job.posted_date == "2026-06-01"


@respx.mock
def test_greenhouse_search_defaults_employer_to_board_token():
    respx.get("https://boards-api.greenhouse.io/v1/boards/firmax/jobs").mock(
        return_value=httpx.Response(200, json=GREENHOUSE_LIST_RESPONSE)
    )
    with GreenhouseSource() as source:
        jobs = source.search(board_token="firmax")
    assert len(jobs) == 2
    assert all(j.employer == "firmax" for j in jobs)


@respx.mock
def test_greenhouse_search_filters_by_location():
    respx.get("https://boards-api.greenhouse.io/v1/boards/firmax/jobs").mock(
        return_value=httpx.Response(200, json=GREENHOUSE_LIST_RESPONSE)
    )
    with GreenhouseSource() as source:
        jobs = source.search(board_token="firmax", location="hamburg")
    assert len(jobs) == 1
    assert jobs[0].title == "Senior Sales Manager"


@respx.mock
def test_greenhouse_fetch_details_uses_cache_from_search():
    respx.get("https://boards-api.greenhouse.io/v1/boards/firmax/jobs").mock(
        return_value=httpx.Response(200, json=GREENHOUSE_LIST_RESPONSE)
    )
    detail_route = respx.get("https://boards-api.greenhouse.io/v1/boards/firmax/jobs/127817").mock(
        return_value=httpx.Response(200, json={"content": "should not be fetched"})
    )
    with GreenhouseSource() as source:
        jobs = source.search(board_token="firmax", keywords="KI")
        description = source.fetch_details(jobs[0].refnr)

    assert "Werkstudenten" in description
    assert detail_route.call_count == 0  # served from cache, no re-fetch


@respx.mock
def test_greenhouse_fetch_details_refetches_on_cache_miss():
    detail_route = respx.get("https://boards-api.greenhouse.io/v1/boards/firmax/jobs/999").mock(
        return_value=httpx.Response(200, json={"content": "<p>Fetched fresh.</p>"})
    )
    with GreenhouseSource() as source:
        description = source.fetch_details("greenhouse:firmax:999")

    assert description == "Fetched fresh."
    assert detail_route.call_count == 1


def test_greenhouse_fetch_details_bad_refnr_returns_empty():
    with GreenhouseSource() as source:
        assert source.fetch_details("not-a-valid-refnr") == ""


# -- LeverSource ------------------------------------------------------------------

LEVER_LIST_RESPONSE = [
    {
        "id": "5ac21346-8e0c-4494-8e7a-3eb92ff77902",
        "text": "Werkstudent Software Engineering",
        "categories": {"location": "Berlin", "team": "Engineering"},
        "descriptionPlain": "Wir suchen einen Werkstudenten fuer unser Engineering-Team.",
        "hostedUrl": "https://jobs.lever.co/firmay/5ac21346-8e0c-4494-8e7a-3eb92ff77902",
    },
    {
        "id": "other-id",
        "text": "Head of Finance",
        "categories": {"location": "Munich", "team": "Finance"},
        "descriptionPlain": "Finance leadership role.",
        "hostedUrl": "https://jobs.lever.co/firmay/other-id",
    },
]


@respx.mock
def test_lever_search_filters_by_keywords():
    respx.get("https://api.lever.co/v0/postings/firmay").mock(
        return_value=httpx.Response(200, json=LEVER_LIST_RESPONSE)
    )
    with LeverSource() as source:
        jobs = source.search(site="firmay", employer="Firma Y GmbH", keywords="Werkstudent")

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Werkstudent Software Engineering"
    assert job.employer == "Firma Y GmbH"
    assert job.location == "Berlin"
    assert job.refnr == "lever:firmay:5ac21346-8e0c-4494-8e7a-3eb92ff77902"
    assert job.url.endswith("5ac21346-8e0c-4494-8e7a-3eb92ff77902")


@respx.mock
def test_lever_search_passes_through_server_side_filters():
    route = respx.get("https://api.lever.co/v0/postings/firmay").mock(
        return_value=httpx.Response(200, json=LEVER_LIST_RESPONSE)
    )
    with LeverSource() as source:
        source.search(site="firmay", location="Berlin", team="Engineering")

    request = route.calls[0].request
    assert request.url.params["location"] == "Berlin"
    assert request.url.params["team"] == "Engineering"


@respx.mock
def test_lever_fetch_details_uses_cache_from_search():
    respx.get("https://api.lever.co/v0/postings/firmay").mock(
        return_value=httpx.Response(200, json=LEVER_LIST_RESPONSE)
    )
    detail_route = respx.get(
        "https://api.lever.co/v0/postings/firmay/5ac21346-8e0c-4494-8e7a-3eb92ff77902"
    ).mock(return_value=httpx.Response(200, json={"descriptionPlain": "should not be fetched"}))

    with LeverSource() as source:
        jobs = source.search(site="firmay", keywords="Werkstudent")
        description = source.fetch_details(jobs[0].refnr)

    assert "Engineering-Team" in description
    assert detail_route.call_count == 0


@respx.mock
def test_lever_fetch_details_refetches_on_cache_miss():
    detail_route = respx.get("https://api.lever.co/v0/postings/firmay/xyz").mock(
        return_value=httpx.Response(200, json={"descriptionPlain": "Fetched fresh."})
    )
    with LeverSource() as source:
        description = source.fetch_details("lever:firmay:xyz")

    assert description == "Fetched fresh."
    assert detail_route.call_count == 1


# -- PersonioSource ---------------------------------------------------------------

PERSONIO_XML_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
<position>
    <id>2683637</id>
    <subcompany>Firma Z GmbH</subcompany>
    <office>Duesseldorf</office>
    <department>Entwicklung</department>
    <name>Werkstudent KI, Duesseldorf</name>
    <jobDescriptions>
        <jobDescription>
            <name>Deine Aufgaben:</name>
            <value><![CDATA[<p>Du unterstuetzt unser <b>KI-Team</b>.</p>]]></value>
        </jobDescription>
        <jobDescription>
            <name>Dein Profil:</name>
            <value><![CDATA[Python-Kenntnisse von Vorteil.]]></value>
        </jobDescription>
    </jobDescriptions>
    <employmentType>permanent</employmentType>
    <createdAt>2026-06-23T14:06:26+00:00</createdAt>
</position>
<position>
    <id>2683999</id>
    <subcompany>Firma Z GmbH</subcompany>
    <office>Muenchen</office>
    <department>Vertrieb</department>
    <name>Sales Manager, Muenchen</name>
    <jobDescriptions>
        <jobDescription>
            <name>Aufgaben:</name>
            <value><![CDATA[Vertrieb von Softwareloesungen.]]></value>
        </jobDescription>
    </jobDescriptions>
    <employmentType>permanent</employmentType>
    <createdAt>2026-06-20T09:00:00+00:00</createdAt>
</position>
</workzag-jobs>
"""


@respx.mock
def test_personio_search_parses_xml_and_filters_by_keywords():
    respx.get("https://firmaz.jobs.personio.de/xml").mock(
        return_value=httpx.Response(200, text=PERSONIO_XML_RESPONSE)
    )
    with PersonioSource() as source:
        jobs = source.search(company="firmaz", keywords="KI")

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Werkstudent KI, Duesseldorf"
    assert job.employer == "Firma Z GmbH"
    assert job.location == "Duesseldorf"
    assert job.profession == "Entwicklung"
    assert job.refnr == "personio:firmaz:2683637"
    assert job.posted_date == "2026-06-23"
    assert "KI-Team" in job.description
    assert "Python-Kenntnisse" in job.description
    assert job.url == "https://firmaz.jobs.personio.de/job/2683637?language=de"


@respx.mock
def test_personio_search_filters_by_location():
    respx.get("https://firmaz.jobs.personio.de/xml").mock(
        return_value=httpx.Response(200, text=PERSONIO_XML_RESPONSE)
    )
    with PersonioSource() as source:
        jobs = source.search(company="firmaz", location="muenchen")
    assert len(jobs) == 1
    assert jobs[0].title == "Sales Manager, Muenchen"


@respx.mock
def test_personio_employer_defaults_to_subcompany_then_company(monkeypatch):
    xml_no_subcompany = PERSONIO_XML_RESPONSE.replace(
        "<subcompany>Firma Z GmbH</subcompany>", "<subcompany></subcompany>"
    )
    respx.get("https://firmaz.jobs.personio.de/xml").mock(
        return_value=httpx.Response(200, text=xml_no_subcompany)
    )
    with PersonioSource() as source:
        jobs = source.search(company="firmaz", keywords="KI")
    assert jobs[0].employer == "firmaz"


@respx.mock
def test_personio_fetch_details_uses_cache_from_search():
    route = respx.get("https://firmaz.jobs.personio.de/xml").mock(
        return_value=httpx.Response(200, text=PERSONIO_XML_RESPONSE)
    )
    with PersonioSource() as source:
        jobs = source.search(company="firmaz", keywords="KI")
        description = source.fetch_details(jobs[0].refnr)

    assert "KI-Team" in description
    assert route.call_count == 1  # only the original search call, no extra fetch


@respx.mock
def test_personio_fetch_details_cache_miss_re_runs_search():
    route = respx.get("https://firmaz.jobs.personio.de/xml").mock(
        return_value=httpx.Response(200, text=PERSONIO_XML_RESPONSE)
    )
    with PersonioSource() as source:
        description = source.fetch_details("personio:firmaz:2683637")

    assert "KI-Team" in description
    assert route.call_count == 1  # re-ran search() once to repopulate the cache


@respx.mock
def test_personio_search_handles_malformed_xml_gracefully():
    respx.get("https://firmaz.jobs.personio.de/xml").mock(
        return_value=httpx.Response(200, text="<not><valid xml")
    )
    with PersonioSource() as source:
        jobs = source.search(company="firmaz")
    assert jobs == []
