"""
arbeitsagentur — Wrapper around the (community-documented) Bundesagentur
für Arbeit Jobsuche REST API.

Endpoint and X-API-Key documented at https://github.com/bundesAPI/jobsuche-api
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("arbeitsagentur")

API_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4"
API_KEY = "jobboerse-jobsuche"  # public key used by jobsuche.de itself

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclass
class Job:
    """Normalised representation of one Stellenangebot."""

    refnr: str
    title: str
    employer: str
    location: str
    posted_date: str
    profession: str = ""
    url: str = ""
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict) -> "Job":
        ort = raw.get("arbeitsort") or {}
        location = ort.get("ort") or ""
        if ort.get("plz"):
            location = f"{ort['plz']} {location}".strip()
        refnr = raw.get("refnr", "")
        external_url = raw.get("externeUrl", "")
        agentur_url = (
            f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"
            if refnr
            else ""
        )
        return cls(
            refnr=refnr,
            title=raw.get("titel", "").strip(),
            employer=raw.get("arbeitgeber", "").strip(),
            location=location,
            posted_date=raw.get("aktuelleVeroeffentlichungsdatum", ""),
            profession=raw.get("beruf", "").strip(),
            url=external_url or agentur_url,
            description=raw.get("beruf", "") + " — " + raw.get("titel", ""),
            extra={"raw": raw},
        )


class ArbeitsagenturClient:
    """Thin wrapper. One method to search, one method to fetch full details."""

    def __init__(self, *, base_url: str = API_BASE, api_key: str = API_KEY) -> None:
        self.base_url = base_url
        self._client = httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
        )

    def __enter__(self) -> "ArbeitsagenturClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- search ------------------------------------------------------------
    def search(
        self,
        *,
        was: str = "",
        wo: str = "",
        umkreis: int = 0,
        angebotsart: int = 1,
        size: int = 50,
        veroeffentlichtseit: int | None = None,
        pav: bool = False,
        max_pages: int = 10,
    ) -> list[Job]:
        """Run a search query, paging through results, and return all hits.

        Stops once a page comes back short (the last page) or `max_pages`
        is hit — a safety cap so an overly broad query can't page forever.
        """
        jobs: list[Job] = []
        for page in range(1, max_pages + 1):
            batch = self._search_page(
                was=was,
                wo=wo,
                umkreis=umkreis,
                angebotsart=angebotsart,
                page=page,
                size=size,
                veroeffentlichtseit=veroeffentlichtseit,
                pav=pav,
            )
            jobs.extend(batch)
            if len(batch) < size:
                break
        else:
            log.warning("Hit max_pages=%d for query was=%r wo=%r — results may be truncated.", max_pages, was, wo)
        return jobs

    def _search_page(
        self,
        *,
        was: str,
        wo: str,
        umkreis: int,
        angebotsart: int,
        page: int,
        size: int,
        veroeffentlichtseit: int | None,
        pav: bool,
    ) -> list[Job]:
        params: dict[str, Any] = {
            "was": was,
            "wo": wo,
            "umkreis": umkreis,
            "angebotsart": angebotsart,
            "page": page,
            "size": size,
            "pav": str(pav).lower(),
        }
        if veroeffentlichtseit is not None:
            params["veroeffentlichtseit"] = veroeffentlichtseit
        # Drop empty params — API tolerates missing better than empty strings.
        params = {k: v for k, v in params.items() if v not in ("", None)}

        log.debug("GET %s/jobs %s", self.base_url, params)
        r = self._client.get(f"{self.base_url}/jobs", params=params)
        r.raise_for_status()
        data = r.json()
        return [Job.from_api(item) for item in data.get("stellenangebote", [])]

    # -- details -----------------------------------------------------------
    def fetch_details(self, refnr: str) -> str:
        """Fetch the full Stellenbeschreibung for a refnr.

        Returns the cleaned text body or empty string on failure.
        """
        if not refnr:
            return ""
        encoded = base64.b64encode(refnr.encode()).decode()
        url = f"{self.base_url}/jobdetails/{encoded}"
        try:
            r = self._client.get(url)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as exc:
            log.warning("details fetch failed for %s: %s", refnr, exc)
            return ""

        parts = []
        if isinstance(data, dict):
            if data.get("stellenbeschreibung"):
                parts.append(data["stellenbeschreibung"])
            if data.get("titel"):
                parts.append(f"Titel: {data['titel']}")
            if data.get("beruf"):
                parts.append(f"Beruf: {data['beruf']}")
        return "\n\n".join(p for p in parts if p)
