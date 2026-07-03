"""
ats_sources — JobSource implementations for the applicant-tracking systems
(ATS) that many companies' "careers" pages actually run on under the hood,
as opposed to a portal like arbeitsagentur.de or LinkedIn.

Why these three: Greenhouse, Lever, and Personio all publish a public,
official, no-auth JSON/XML feed *meant* for exactly this kind of external
read — this is not scraping, and not the legally/technically fraught
territory that LinkedIn (no public API for this use case, ToS-hostile to
scraping) or an arbitrary hand-rolled company career page (no stable
structure, breaks on every redesign) would be. If a company you're
targeting isn't on one of these, check its careers page URL/HTML for
"greenhouse.io", "lever.co", or "personio" — many are, especially Personio
in the German market.

Each source's `search()` fetches the *entire* list of open postings for one
board/site/company (the public APIs don't support arbeitrary full-text
search server-side) and filters client-side by keyword/location — the same
shape as arbeitsagentur.py's `was`/`wo`, just resolved locally instead of by
the remote API. All three APIs return the full job description in the list
response itself, so `fetch_details()` is normally a cache lookup, not a
second HTTP round-trip — with a live re-fetch fallback for the (unusual)
case where fetch_details is called without a preceding search() in the same
process.

queries.json usage (see README "Adding a new job source"):
    {"label": "Firma X Werkstudent", "source": "greenhouse",
     "params": {"board_token": "firmax", "employer": "Firma X GmbH",
                "keywords": "Werkstudent Python"}}
    {"label": "Firma Y Werkstudent", "source": "lever",
     "params": {"site": "firmay", "employer": "Firma Y GmbH",
                "keywords": "Werkstudent", "location": "Berlin"}}
    {"label": "Firma Z Werkstudent", "source": "personio",
     "params": {"company": "firmaz", "employer": "Firma Z GmbH",
                "keywords": "Werkstudent"}}
"""
from __future__ import annotations

import logging
import re
import time
from html import unescape
from xml.etree import ElementTree

import httpx

from arbeitsagentur import Job
from job_source import JobSource

log = logging.getLogger("ats_sources")

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(raw: str | None) -> str:
    """Rough HTML→plaintext for LLM consumption. Doesn't need to be
    pixel-perfect, just readable — good enough is fine here."""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", unescape(raw))
    return _WS_RE.sub(" ", text).strip()


def _matches_keywords(haystack: str, keywords: str) -> bool:
    """All whitespace-separated words in `keywords` must appear somewhere in
    `haystack` as a whole word (case-insensitive, AND semantics) — narrower
    than arbeitsagentur's `was`, but these APIs have no server-side
    full-text search to delegate to.

    Word-boundary matching, not plain substring: a naive `"ki" in haystack`
    would false-positive on ordinary words like "skills" or "Praktikum"."""
    if not keywords:
        return True
    haystack_lower = haystack.lower()
    return all(
        re.search(rf"\b{re.escape(word.lower())}\b", haystack_lower)
        for word in keywords.split()
    )


def _get_with_retry(
    client: httpx.Client, url: str, *, params: dict | None = None
) -> httpx.Response:
    """Same retry contract as arbeitsagentur.py's client: exponential backoff
    on network errors / 5xx, fail fast on 4xx. Kept as an independent copy
    rather than a shared import so this module doesn't reach into
    arbeitsagentur.py's internals for an unrelated client."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.get(url, params=params)
        except httpx.TransportError as exc:
            last_exc = exc
        else:
            if r.status_code < 500:
                r.raise_for_status()
                return r
            last_exc = httpx.HTTPStatusError(
                f"Server error {r.status_code}", request=r.request, response=r
            )
        if attempt < MAX_RETRIES:
            sleep_for = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            log.warning(
                "GET %s failed (attempt %d/%d): %s — retrying in %.1fs",
                url, attempt, MAX_RETRIES, last_exc, sleep_for,
            )
            time.sleep(sleep_for)
    assert last_exc is not None
    raise last_exc


class GreenhouseSource(JobSource):
    """https://developers.greenhouse.io/job-board.html — public, no-auth."""

    name = "greenhouse"
    BASE_URL = "https://boards-api.greenhouse.io/v1/boards"

    def __init__(self, *, timeout: httpx.Timeout = DEFAULT_TIMEOUT) -> None:
        self._client = httpx.Client(timeout=timeout)
        self._description_cache: dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    def search(
        self, *, board_token: str, employer: str = "", keywords: str = "", location: str = "",
    ) -> list[Job]:
        url = f"{self.BASE_URL}/{board_token}/jobs"
        r = _get_with_retry(self._client, url, params={"content": "true"})
        data = r.json()

        jobs: list[Job] = []
        for item in data.get("jobs", []):
            title = item.get("title", "")
            loc_name = (item.get("location") or {}).get("name", "")
            description = _strip_html(item.get("content", ""))

            if location and location.lower() not in loc_name.lower():
                continue
            if not _matches_keywords(f"{title} {description}", keywords):
                continue

            refnr = f"greenhouse:{board_token}:{item['id']}"
            self._description_cache[refnr] = description
            jobs.append(Job(
                refnr=refnr,
                title=title,
                employer=employer or board_token,
                location=loc_name,
                posted_date=(item.get("updated_at") or "")[:10],
                url=item.get("absolute_url", ""),
                description=description,
            ))
        return jobs

    def fetch_details(self, refnr: str) -> str:
        if refnr in self._description_cache:
            return self._description_cache[refnr]
        try:
            _, board_token, job_id = refnr.split(":", 2)
        except ValueError:
            return ""
        try:
            r = _get_with_retry(self._client, f"{self.BASE_URL}/{board_token}/jobs/{job_id}")
        except httpx.HTTPError:
            log.warning("Greenhouse details fetch failed for %s", refnr)
            return ""
        return _strip_html(r.json().get("content", ""))


class LeverSource(JobSource):
    """https://github.com/lever/postings-api — public, no-auth JSON API."""

    name = "lever"
    BASE_URL = "https://api.lever.co/v0/postings"

    def __init__(self, *, timeout: httpx.Timeout = DEFAULT_TIMEOUT) -> None:
        self._client = httpx.Client(timeout=timeout)
        self._description_cache: dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    def search(
        self, *, site: str, employer: str = "", keywords: str = "", location: str = "",
        team: str = "", commitment: str = "",
    ) -> list[Job]:
        url = f"{self.BASE_URL}/{site}"
        params: dict[str, str] = {"mode": "json"}
        # Lever supports server-side filtering on these (OR'ed if repeated;
        # we only ever pass one value each, which is an exact-match filter).
        if location:
            params["location"] = location
        if team:
            params["team"] = team
        if commitment:
            params["commitment"] = commitment

        r = _get_with_retry(self._client, url, params=params)
        postings = r.json()

        jobs: list[Job] = []
        for item in postings:
            title = item.get("text", "")
            description = item.get("descriptionPlain", "") or _strip_html(item.get("description", ""))
            if not _matches_keywords(f"{title} {description}", keywords):
                continue

            categories = item.get("categories") or {}
            refnr = f"lever:{site}:{item['id']}"
            self._description_cache[refnr] = description
            jobs.append(Job(
                refnr=refnr,
                title=title,
                employer=employer or site,
                location=categories.get("location", ""),
                posted_date="",  # Lever's postings API doesn't expose a creation date
                url=item.get("hostedUrl", ""),
                description=description,
            ))
        return jobs

    def fetch_details(self, refnr: str) -> str:
        if refnr in self._description_cache:
            return self._description_cache[refnr]
        try:
            _, site, posting_id = refnr.split(":", 2)
        except ValueError:
            return ""
        try:
            r = _get_with_retry(
                self._client, f"{self.BASE_URL}/{site}/{posting_id}", params={"mode": "json"}
            )
        except httpx.HTTPError:
            log.warning("Lever details fetch failed for %s", refnr)
            return ""
        data = r.json()
        return data.get("descriptionPlain", "") or _strip_html(data.get("description", ""))


class PersonioSource(JobSource):
    """https://developer.personio.de/docs/retrieving-open-job-positions —
    public, no-auth XML feed. Very common for German SMEs/Mittelstand."""

    name = "personio"
    URL_TEMPLATE = "https://{company}.jobs.personio.de/xml"

    def __init__(self, *, timeout: httpx.Timeout = DEFAULT_TIMEOUT) -> None:
        self._client = httpx.Client(timeout=timeout)
        self._description_cache: dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    def search(
        self, *, company: str, employer: str = "", keywords: str = "", location: str = "",
        language: str = "de",
    ) -> list[Job]:
        url = self.URL_TEMPLATE.format(company=company)
        r = _get_with_retry(self._client, url, params={"language": language})

        try:
            root = ElementTree.fromstring(r.text)
        except ElementTree.ParseError:
            log.warning("Personio feed for %r returned unparsable XML.", company)
            return []

        jobs: list[Job] = []
        for position in root.findall("position"):
            job_id = (position.findtext("id") or "").strip()
            title = (position.findtext("name") or "").strip()
            office = (position.findtext("office") or "").strip()
            description = _describe_personio_position(position)

            if location and location.lower() not in office.lower():
                continue
            if not _matches_keywords(f"{title} {description}", keywords):
                continue

            refnr = f"personio:{company}:{job_id}"
            self._description_cache[refnr] = description
            jobs.append(Job(
                refnr=refnr,
                title=title,
                employer=employer or (position.findtext("subcompany") or "").strip() or company,
                location=office,
                posted_date=(position.findtext("createdAt") or "")[:10],
                profession=(position.findtext("department") or "").strip(),
                url=f"https://{company}.jobs.personio.de/job/{job_id}?language={language}" if job_id else "",
                description=description,
            ))
        return jobs

    def fetch_details(self, refnr: str) -> str:
        if refnr in self._description_cache:
            return self._description_cache[refnr]
        # Re-fetching a single position isn't supported by the XML feed —
        # it only exposes the full list. Re-run search() and hope the cache
        # picks it up; if the job has since closed, this correctly yields "".
        try:
            _, company, _job_id = refnr.split(":", 2)
        except ValueError:
            return ""
        self.search(company=company)
        return self._description_cache.get(refnr, "")


def _describe_personio_position(position: ElementTree.Element) -> str:
    """Concatenate every <jobDescription> section ('Aufgaben', 'Profil',
    'Wir bieten', ...) into one plaintext description, in feed order —
    Personio doesn't have a single 'description' field, it's split into
    company-defined sections."""
    parts = []
    for job_description in position.findall("jobDescriptions/jobDescription"):
        heading = (job_description.findtext("name") or "").strip()
        body = _strip_html(job_description.findtext("value") or "")
        if body:
            parts.append(f"{heading}\n{body}" if heading else body)
    return "\n\n".join(parts)
