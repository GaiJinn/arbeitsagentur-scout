"""
job_source — abstraction over "a place jobs come from".

arbeitsagentur.py's ArbeitsagenturClient is the only implementation today,
but scout.py's pipeline is written against this interface rather than
against ArbeitsagenturClient directly. That's what would let a future
StepStone/Indeed/LinkedIn client slot in later (see README roadmap) without
touching scout.py's orchestration logic — just a new class implementing
JobSource, registered in scout.py's SOURCE_REGISTRY, and a `"source": "..."`
field on the relevant queries.json entries.

Uses TYPE_CHECKING to avoid a circular import with arbeitsagentur.py (which
defines Job and implements this interface).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arbeitsagentur import Job


class JobSource(ABC):
    """One searchable job board. Every concrete source (arbeitsagentur,
    eventually StepStone/Indeed/...) implements this."""

    #: short, stable identifier used in queries.json's "source" field and
    #: in logs. Override in subclasses.
    name: str = "unknown"

    @abstractmethod
    def search(self, **params: object) -> list["Job"]:
        """Run one search query and return normalised Job results.

        `params` is whatever this source's queries.json entries pass through
        — arbeitsagentur's are `was`/`wo`/`umkreis`/etc; a different source
        would define its own param names.
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_details(self, refnr: str) -> str:
        """Fetch the full description text for one job.

        Must return "" (not raise) on failure — callers treat an empty
        string as "no description available" and skip LLM scoring for that
        job rather than scoring on a thin fallback.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Release any held resources (HTTP client, etc). Override if needed."""

    def __enter__(self) -> "JobSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
