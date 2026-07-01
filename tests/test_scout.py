"""
Unit tests for scout.py's core pipeline (_collect_new_jobs), independent of
the integration test in test_integration.py which drives main() end-to-end
over mocked HTTP.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("GROQ_API_KEY", "")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

import scout  # noqa: E402
from analyzer import JobScore  # noqa: E402
from arbeitsagentur import Job  # noqa: E402


def make_job(refnr="job-1") -> Job:
    return Job(
        refnr=refnr,
        title="Werkstudent KI",
        employer="Beispiel GmbH",
        location="Düsseldorf",
        posted_date="2026-06-01",
        profession="Informatiker",
        description="Informatiker — Werkstudent KI",  # the thin from_api default
    )


class FakeClient:
    def __init__(self, jobs, details: dict[str, str]):
        self._jobs = jobs
        self._details = details

    def search(self, **_):
        return self._jobs

    def fetch_details(self, refnr):
        return self._details.get(refnr, "")


class FakeStorage:
    def __init__(self):
        self.seen: set[str] = set()
        self.saved: list[tuple] = []

    def has_job(self, refnr):
        return refnr in self.seen

    def save(self, job, score):
        self.seen.add(job.refnr)
        self.saved.append((job, score))


class FakeAnalyzer:
    """Records which jobs it was asked to score, always returns a fixed score."""

    def __init__(self, fixed_score=8):
        self.scored_refnrs: list[str] = []
        self.fixed_score = fixed_score

    def score(self, job) -> JobScore:
        self.scored_refnrs.append(job.refnr)
        return JobScore(score=self.fixed_score, summary="ok")


def _patch_queries(monkeypatch, queries):
    monkeypatch.setattr(scout, "SEARCH_QUERIES", queries)


def _sources(client) -> dict[str, object]:
    return {"arbeitsagentur": client}


def test_skips_already_seen_jobs(monkeypatch):
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])
    storage = FakeStorage()
    storage.seen.add("job-1")
    client = FakeClient([make_job("job-1")], details={})
    analyzer = FakeAnalyzer()

    new_jobs = scout._collect_new_jobs(_sources(client), storage, analyzer)

    assert new_jobs == []
    assert analyzer.scored_refnrs == []


def test_scores_job_when_details_available(monkeypatch):
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])
    storage = FakeStorage()
    client = FakeClient(
        [make_job("job-1")],
        details={"job-1": "Vollständige Stellenbeschreibung mit viel Kontext."},
    )
    analyzer = FakeAnalyzer(fixed_score=9)

    new_jobs = scout._collect_new_jobs(_sources(client), storage, analyzer)

    assert len(new_jobs) == 1
    job, score = new_jobs[0]
    assert analyzer.scored_refnrs == ["job-1"]
    assert job.description == "Vollständige Stellenbeschreibung mit viel Kontext."
    assert score.score == 9
    assert storage.saved[0][1].score == 9


def test_skips_scoring_when_details_fetch_returns_empty(monkeypatch):
    """A failed detail fetch must NOT fall back to scoring on the thin
    'beruf — titel' description — it should skip the LLM call entirely and
    save the job unscored so it can be reconsidered without a bad score."""
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])
    storage = FakeStorage()
    client = FakeClient([make_job("job-1")], details={})  # no detail text
    analyzer = FakeAnalyzer()

    new_jobs = scout._collect_new_jobs(_sources(client), storage, analyzer)

    assert len(new_jobs) == 1
    job, score = new_jobs[0]
    assert score is None
    assert analyzer.scored_refnrs == []  # never called
    assert job.description == "Informatiker — Werkstudent KI"  # untouched fallback
    assert storage.saved[0][1] is None


def test_skips_scoring_when_details_fetch_raises(monkeypatch):
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])
    storage = FakeStorage()

    class RaisingClient(FakeClient):
        def fetch_details(self, refnr):
            raise RuntimeError("boom")

    client = RaisingClient([make_job("job-1")], details={})
    analyzer = FakeAnalyzer()

    new_jobs = scout._collect_new_jobs(_sources(client), storage, analyzer)

    assert len(new_jobs) == 1
    _, score = new_jobs[0]
    assert score is None
    assert analyzer.scored_refnrs == []


def test_no_analyzer_saves_job_unscored(monkeypatch):
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])
    storage = FakeStorage()
    client = FakeClient([make_job("job-1")], details={"job-1": "full text"})

    new_jobs = scout._collect_new_jobs(_sources(client), storage, analyzer=None)

    assert len(new_jobs) == 1
    _, score = new_jobs[0]
    assert score is None


def test_search_failure_for_one_query_does_not_abort_others(monkeypatch):
    class FlakyClient:
        def search(self, **params):
            if params.get("was") == "broken":
                raise RuntimeError("API down")
            return [make_job("job-2")]

        def fetch_details(self, refnr):
            return "full text"

    _patch_queries(monkeypatch, [
        {"label": "broken query", "params": {"was": "broken"}},
        {"label": "ok query", "params": {"was": "fine"}},
    ])
    storage = FakeStorage()
    analyzer = FakeAnalyzer()

    new_jobs = scout._collect_new_jobs(_sources(FlakyClient()), storage, analyzer)

    assert len(new_jobs) == 1
    assert new_jobs[0][0].refnr == "job-2"


def test_query_with_unknown_source_is_skipped_not_fatal(monkeypatch):
    _patch_queries(monkeypatch, [
        {"label": "stepstone query", "params": {}, "source": "stepstone"},
        {"label": "ok query", "params": {}},
    ])
    storage = FakeStorage()
    client = FakeClient([make_job("job-3")], details={"job-3": "full text"})
    analyzer = FakeAnalyzer()

    # Only "arbeitsagentur" is configured — "stepstone" isn't in `sources`.
    new_jobs = scout._collect_new_jobs(_sources(client), storage, analyzer)

    assert len(new_jobs) == 1
    assert new_jobs[0][0].refnr == "job-3"
